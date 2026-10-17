"""
Daily watch report for the tournament bot (run by .github/workflows/watch_bot.yaml).

Counts the last 24 hours of run_bot_on_tournament.yaml runs, reads each run's log
for the per-question lines that ForecastBot.log_report_summary prints after
"Bot: <name>" ("✅ URL: ..." / "❌ Exception: ...") and the "FAILED_QUESTION ..."
lines that bot_helpers.print_failed_questions prints, and posts a short Japanese
report as a comment on the issue titled REPORT_ISSUE_TITLE (created if missing).

Standard library only, so the workflow needs no install step.
Run with --dry-run to print the report instead of posting it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TARGET_WORKFLOW = "run_bot_on_tournament.yaml"
SELF_WORKFLOW = "watch_bot.yaml"
REPORT_ISSUE_TITLE = "見張りの日報"
MENTION = "@bunzo-llc"
WINDOW = timedelta(hours=24)
STALL_AFTER = timedelta(hours=2)
# GitHub disables scheduled workflows in a public repository after 60 days
# without repository activity. Warn well before that.
INACTIVITY_LIMIT_DAYS = 60
INACTIVITY_WARN_DAYS = 50
JST = ZoneInfo("Asia/Tokyo")

_TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z ")
# Printed by bot_helpers.print_failed_questions; title comes last and may hold spaces.
_FAILED_QUESTION = re.compile(
    r"^FAILED_QUESTION stage=(?P<stage>\S+) post_id=(?P<post_id>\S+) "
    r"question_id=(?P<question_id>\S+) url=(?P<url>\S+) title=(?P<title>.*)$"
)
# What each stage means, in the report's words.
STAGE_LABELS = {
    "prediction": "予測が出ていない",
    "partial_prediction": "予測が一部しか出ていない",
    "comment": "予測は出たが、コメントが出ていない",
    "after_posting": "予測もコメントも出たあとで失敗した",
}


@dataclass
class FailedQuestionLine:
    stage: str
    post_id: str
    question_id: str
    url: str
    title: str
    run_url: str = ""


@dataclass
class LogCounts:
    succeeded: int = 0
    failed: int = 0
    no_new_questions: bool = False
    failed_questions: list[FailedQuestionLine] = field(default_factory=list)


@dataclass
class Run:
    id: int
    event: str
    status: str
    conclusion: str | None
    created_at: datetime
    started_at: datetime
    updated_at: datetime
    html_url: str
    log: LogCounts | None = None  # None when the log could not be read


@dataclass
class Snapshot:
    now: datetime
    target_state: str
    self_state: str
    runs_in_window: list[Run]
    latest_run_at: datetime | None
    last_push_at: datetime | None
    enable_actions: list[str] = field(default_factory=list)
    log_errors: int = 0


def parse_log(text: str) -> LogCounts:
    counts = LogCounts()
    in_summary = False
    for raw in text.splitlines():
        line = _TIMESTAMP_PREFIX.sub("", raw).strip()
        if "No new questions to forecast" in line:
            counts.no_new_questions = True
        match = _FAILED_QUESTION.match(line)
        if match:
            counts.failed_questions.append(FailedQuestionLine(**match.groupdict()))
            continue
        if line.startswith("Bot: "):
            in_summary = True
            continue
        if not in_summary:
            continue
        if line.startswith("✅ URL:"):
            counts.succeeded += 1
        elif line.startswith("❌ Exception:"):
            counts.failed += 1
        elif line.startswith("Stats for passing reports"):
            in_summary = False
    return counts


def judge(snap: Snapshot) -> tuple[bool, list[str]]:
    """Returns (is_ok, problem lines). Zero questions alone is not a problem."""
    problems: list[str] = []
    runs = snap.runs_in_window
    failed_runs = [r for r in runs if r.conclusion not in (None, "success", "cancelled", "skipped")]
    if failed_runs:
        problems.append(f"失敗した実行が {len(failed_runs)} 回あります")
    failed_questions = sum(r.log.failed for r in runs if r.log)
    if failed_questions:
        problems.append(f"失敗した問いが {failed_questions} 件あります")
    by_stage: dict[str, int] = {}
    for f in (f for r in runs if r.log for f in r.log.failed_questions):
        by_stage[f.stage] = by_stage.get(f.stage, 0) + 1
    for stage, n in by_stage.items():
        problems.append(f"{STAGE_LABELS.get(stage, stage)}問いが {n} 件")
    if snap.latest_run_at is None:
        problems.append("実行の記録が1件もありません（止まっている疑い）")
    elif snap.now - snap.latest_run_at > STALL_AFTER:
        hours = (snap.now - snap.latest_run_at).total_seconds() / 3600
        problems.append(f"最後の実行から {hours:.1f} 時間たっています（止まっている疑い）")
    if snap.target_state != "active":
        problems.append(f"本大会の係のワークフローが有効ではありません（{snap.target_state}）")
    if snap.self_state not in ("active", "unknown"):
        problems.append(f"見張りの係のワークフローが有効ではありません（{snap.self_state}）")
    if snap.last_push_at is not None:
        idle_days = (snap.now - snap.last_push_at).days
        if idle_days >= INACTIVITY_WARN_DAYS:
            problems.append(
                f"最後の push から {idle_days} 日です。{INACTIVITY_LIMIT_DAYS} 日で時刻の係が自動で止まる恐れがあります"
            )
    return (not problems, problems)


def _fmt_jst(dt: datetime | None) -> str:
    return dt.astimezone(JST).strftime("%m/%d %H:%M") if dt else "なし"


def render(snap: Snapshot) -> str:
    ok, problems = judge(snap)
    runs = snap.runs_in_window
    success = sum(r.conclusion == "success" for r in runs)
    cancelled = sum(r.conclusion == "cancelled" for r in runs)
    running = sum(r.conclusion is None for r in runs)
    failed = len(runs) - success - cancelled - running - sum(r.conclusion == "skipped" for r in runs)
    q_ok = sum(r.log.succeeded for r in runs if r.log)
    q_ng = sum(r.log.failed for r in runs if r.log)
    no_new = sum(1 for r in runs if r.log and r.log.no_new_questions)
    finished = [r for r in runs if r.conclusion is not None]
    longest = max(finished, key=lambda r: r.updated_at - r.started_at, default=None)
    idle_days = (snap.now - snap.last_push_at).days if snap.last_push_at else None

    head = "🟢 異常なし" if ok else "🔴 " + "／".join(problems)
    lines = [
        f"{head}　{MENTION}",
        "",
        f"本大会の係の直近24時間（{_fmt_jst(snap.now - WINDOW)}〜{_fmt_jst(snap.now)} 日本時間）",
        f"- 実行 {len(runs)} 回：成功 {success}・失敗 {failed}・取り消し {cancelled}"
        + (f"・実行中 {running}" if running else ""),
        f"- 予測した問い：✅ {q_ok} 件・❌ {q_ng} 件／「新しい問いなし」で終わった回 {no_new}",
    ]
    if longest:
        minutes = (longest.updated_at - longest.started_at).total_seconds() / 60
        lines.append(f"- いちばん長い実行：{minutes:.1f} 分（{longest.html_url}）")
    lines.append(f"- いちばん新しい実行：{_fmt_jst(snap.latest_run_at)}")
    lines.append(f"- ワークフローの状態：本大会 {snap.target_state}・見張り {snap.self_state}")
    if idle_days is not None:
        lines.append(f"- 最後の push から {idle_days} 日（{INACTIVITY_LIMIT_DAYS} 日で時刻の係が止まる恐れ）")
    if snap.log_errors:
        lines.append(f"- ログを読めなかった実行：{snap.log_errors} 回（問いの数に入っていません）")
    failed_lines = [f for r in runs if r.log for f in r.log.failed_questions]
    if failed_lines:
        lines.append("")
        lines.append("失敗した問い")
        for f in failed_lines:
            label = STAGE_LABELS.get(f.stage, f.stage)
            lines.append(
                f"- 【{label}】{f.title}（投稿 ID {f.post_id}・問い ID {f.question_id}）{f.url}　実行：{f.run_url}"
            )
    unexplained = q_ng - len(failed_lines)
    if unexplained > 0:
        lines.append(
            f"- ほかに ❌ が {unexplained} 件あります。問いの ID と段階はログに出ていません（この仕組みより前の実行など）"
        )
    for action in snap.enable_actions:
        lines.append(f"- {action}")
    return "\n".join(lines)


class GitHub:
    def __init__(self, repo: str, token: str | None):
        self.base = f"https://api.github.com/repos/{repo}"
        self.token = token

    def _request(self, method: str, url: str, body: dict | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "watch-bot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def get(self, path: str):
        return self._request("GET", self.base + path)

    def post(self, path: str, body: dict):
        return self._request("POST", self.base + path, body)

    def put(self, path: str):
        return self._request("PUT", self.base + path, {})

    def job_log(self, job_id: int) -> str:
        # The API answers with a redirect to a short-lived pre-signed URL; fetch
        # that URL without our Authorization header.
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(
            f"{self.base}/actions/jobs/{job_id}/logs",
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "watch-bot"},
        )
        try:
            opener.open(req, timeout=60)
            raise RuntimeError("expected a redirect for job logs")
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            location = e.headers["Location"]
        with urllib.request.urlopen(location, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _workflow_state(gh: GitHub, workflow: str) -> str:
    try:
        return gh.get(f"/actions/workflows/{workflow}")["state"]
    except urllib.error.HTTPError as e:
        return "unknown" if e.code == 404 else f"error {e.code}"


def collect(gh: GitHub, now: datetime) -> Snapshot:
    since = now - WINDOW
    runs: list[Run] = []
    latest_run_at: datetime | None = None
    page = 1
    while True:
        data = gh.get(f"/actions/workflows/{TARGET_WORKFLOW}/runs?per_page=100&page={page}")
        batch = data["workflow_runs"]
        for item in batch:
            created = _ts(item["created_at"])
            if latest_run_at is None or created > latest_run_at:
                latest_run_at = created
            if created < since:
                continue
            runs.append(
                Run(
                    id=item["id"],
                    event=item["event"],
                    status=item["status"],
                    conclusion=item["conclusion"],
                    created_at=created,
                    started_at=_ts(item.get("run_started_at") or item["created_at"]),
                    updated_at=_ts(item["updated_at"]),
                    html_url=item["html_url"],
                )
            )
        # Runs come newest first; stop once a page reaches past the window.
        if not batch or _ts(batch[-1]["created_at"]) < since or len(batch) < 100:
            break
        page += 1

    log_errors = 0
    for run in runs:
        if run.conclusion is None:
            continue
        try:
            jobs = gh.get(f"/actions/runs/{run.id}/jobs")["jobs"]
            counts = LogCounts()
            for job in jobs:
                part = parse_log(gh.job_log(job["id"]))
                counts.succeeded += part.succeeded
                counts.failed += part.failed
                counts.no_new_questions |= part.no_new_questions
                for f in part.failed_questions:
                    f.run_url = run.html_url
                counts.failed_questions += part.failed_questions
            run.log = counts
        except Exception as e:  # a missing or expired log should not stop the report
            print(f"could not read log for run {run.id}: {e}", file=sys.stderr)
            log_errors += 1

    repo = gh._request("GET", gh.base)
    return Snapshot(
        now=now,
        target_state=_workflow_state(gh, TARGET_WORKFLOW),
        self_state=_workflow_state(gh, SELF_WORKFLOW),
        runs_in_window=runs,
        latest_run_at=latest_run_at,
        last_push_at=_ts(repo["pushed_at"]) if repo.get("pushed_at") else None,
        log_errors=log_errors,
    )


def keep_workflows_enabled(gh: GitHub, snap: Snapshot) -> None:
    """
    Re-enable a workflow GitHub disabled for inactivity (documented behaviour of
    PUT .../enable). Also re-sends enable for active workflows; whether that resets
    the 60-day inactivity clock is not documented. Never touches a workflow a
    person disabled by hand.
    """
    for workflow, attr in ((TARGET_WORKFLOW, "target_state"), (SELF_WORKFLOW, "self_state")):
        state = getattr(snap, attr)
        if state not in ("active", "disabled_inactivity"):
            continue
        try:
            gh.put(f"/actions/workflows/{workflow}/enable")
        except urllib.error.HTTPError as e:
            snap.enable_actions.append(f"{workflow} の有効化に失敗しました（{e.code}）")
            continue
        if state == "disabled_inactivity":
            snap.enable_actions.append(f"{workflow} は動きが無くて止められていたので、有効に戻しました")


def post_report(gh: GitHub, body: str) -> str:
    issue_number = None
    page = 1
    while issue_number is None:
        issues = gh.get(f"/issues?state=open&per_page=100&page={page}")
        for issue in issues:
            if issue["title"] == REPORT_ISSUE_TITLE and "pull_request" not in issue:
                issue_number = issue["number"]
                break
        if len(issues) < 100:
            break
        page += 1
    if issue_number is None:
        created = gh.post(
            "/issues",
            {
                "title": REPORT_ISSUE_TITLE,
                "body": "見張りの係（.github/workflows/watch_bot.yaml）が毎朝ここに日報を書きます。",
            },
        )
        issue_number = created["number"]
    comment = gh.post(f"/issues/{issue_number}/comments", {"body": body})
    return comment["html_url"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print the report; post nothing, enable nothing")
    args = parser.parse_args()

    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_TOKEN"))
    snap = collect(gh, datetime.now(timezone.utc))
    if not args.dry_run:
        keep_workflows_enabled(gh, snap)
    body = render(snap)
    print(body)
    if args.dry_run:
        return 0
    try:
        url = post_report(gh, body)
    except urllib.error.HTTPError as e:
        if e.code == 410:
            print("::error::Issues are disabled in this repository. Turn on Settings → General → Features → Issues.")
        raise
    print(f"posted: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
