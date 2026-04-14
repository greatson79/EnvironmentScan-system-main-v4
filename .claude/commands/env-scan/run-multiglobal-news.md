---
name: run-multiglobal-news-scan
description: Execute WF4 (Multi&Global-News Environmental Scanning) as a standalone workflow
context: fork
---

# Run Multi&Global-News Environmental Scan (WF4 Standalone)

Execute WF4 independently — direct crawling of 43 global news sites with FSSF classification, Three Horizons tagging, Tipping Point detection, and multilingual translation.
This produces a complete, independently valid report without requiring WF1, WF2, WF3, or integration.

## Usage

```bash
/run-multiglobal-news-scan
```

## What This Command Does

This command invokes the **master-orchestrator** in WF4-only mode:

1. **SOT Validation** — Same startup validation (SOT-001 through SOT-054)
2. **WF4 Execution** — Full 3-phase pipeline on 43 direct news sites only
3. **No WF1/WF2/WF3** — WF1, WF2, and WF3 are skipped entirely
4. **No Integration** — No report merge (WF4 report is the final output)

### WF4 Parameters

| Parameter | Value |
|-----------|-------|
| Source | 43 direct news sites (17 KR + 20 EN + 7 other) |
| Languages | 11 (KO, EN, ZH, JA, DE, FR, ES, RU, PT, AR, HI) |
| FSSF Classification | 8-type signal taxonomy |
| Three Horizons | H1 (0-2yr), H2 (2-7yr), H3 (7yr+) |
| Tipping Point | Critical Slowing Down + Flickering detection |
| Anomaly Detection | Statistical + Structural |
| Translation | Multilingual→EN + EN→KO pipeline |

### WF4 Phases

- **Phase 1**: Crawl 43 news sites (RSS-first with anti-blocking cascade), noise filter, deduplicate
- **Phase 2**: STEEPs + FSSF classify → Impact + Tipping Point → Priority → **Human review (required)**
- **Phase 3**: DB update → Report (multiglobal-news skeleton) → Archive + Alert → **Human approval (required)**

## Checkpoints (2 total)

| # | Step | Type | Command |
|---|------|------|---------|
| 1 | WF4 Step 2.5 | Required | `/review-analysis` |
| 2 | WF4 Step 3.4 | Required | `/approve` or `/revision` |

## Output

- Report: `env-scanning/wf4-multiglobal-news/reports/daily/environmental-scan-{date}.md`
- Database: `env-scanning/wf4-multiglobal-news/signals/database.json`
- Archive: `env-scanning/wf4-multiglobal-news/reports/archive/{year}/{month}/`
- Alerts: `env-scanning/wf4-multiglobal-news/logs/alerts-{date}.json`
- Tipping Points: `env-scanning/wf4-multiglobal-news/analysis/tipping-point-indicators-{date}.json`

## When to Use

- When you need global multilingual news signals only
- When WF1+WF2+WF3 have already been run and you want supplementary global news data
- When WF4 crawling failed during a quadruple scan and you want to retry WF4
- When you need FSSF / Three Horizons / Tipping Point analysis on international news specifically

## Error Handling

- **News site blocked**: CrawlDefender 7-strategy cascade with NetworkGuard (automatic escalation)
- **Low article count** (< 50): Re-crawl with increased delays, then prompt user
- **RSS unavailable**: Automatic fallback to direct HTML parsing
- **All strategies exhausted**: User prompted to configure proxy or abort

## Related Commands

- `/run` - Full quadruple scan (WF1 + WF2 + WF3 + WF4 + Integration)
- `/run-arxiv` - WF2 standalone (arXiv only)
- `/run-naver` - WF3 standalone (Naver News only)
- `/status` - Check workflow progress
- `/approve` - Approve final report

## Version
**Command Version**: 1.0.0
**Last Updated**: 2026-03-02
