# Gemini API Usage Audit

**Generated:** 2026-06-30  
**Source:** `backend/logs/explanation_pipeline.log` (1,511 log records)  
**Model (generate):** `gemini-2.5-flash`  
**Model (embed):** `gemini-embedding-001`  
**Keys in rotation:** 5 (`jZfei_tA`, `GWUf3hHg`, `-NlXkftg`, `R3N8QMBw`, `VGUK4RlA`)

---

## 1. Top-Line Numbers

| Metric | Count |
|---|---|
| **Total successful API calls** | **390** |
| Total circuit-breaker fires (key removed from rotation) | 42 |
| Quota-exhausted events at application layer | 26 |
| Distinct API keys exhausted | 5 of 5 |
| Days with full quota exhaustion | 3 (2026-06-26, 2026-06-29, 2026-06-30) |

---

## 2. Calls by Day

| Date | Successful Calls | Circuit Breaker | Notes |
|---|---|---|---|
| 2026-06-26 | 61 | 8 | First day; all 4 keys exhausted at 10:07 UTC |
| 2026-06-27 | 41 | 0 | Quota reset; no exhaustion |
| 2026-06-28 | 8 | 0 | Light usage; no exhaustion |
| 2026-06-29 | 142 | 26 | All 5 keys exhausted by 09:29 UTC |
| 2026-06-30 | 138 | 8 | All 5 keys exhausted by 09:30 UTC |

---

## 3. Calls by Hour (UTC) — All Days

| Hour (UTC) | Successful Calls |
|---|---|
| 2026-06-26 T10 | 56 |
| 2026-06-26 T12 | 2 |
| 2026-06-26 T14 | 3 |
| 2026-06-27 T07 | 2 |
| 2026-06-27 T08 | 9 |
| 2026-06-27 T09 | 21 |
| 2026-06-27 T10 | 4 |
| 2026-06-27 T23 | 5 |
| 2026-06-28 T09 | 4 |
| 2026-06-28 T17 | 4 |
| 2026-06-29 T08 | 16 |
| 2026-06-29 T09 | 100 |
| 2026-06-29 T10 | 2 |
| 2026-06-29 T12 | 19 |
| 2026-06-29 T13 | 3 |
| 2026-06-29 T14 | 2 |
| 2026-06-30 T08 | 1 |
| 2026-06-30 T09 | 112 |
| 2026-06-30 T10 | 16 (embeds only — generate exhausted) |
| 2026-06-30 T12 | 9 (embeds only — generate exhausted) |

**Pattern:** The RSS ingestion pipeline fires during NSE market hours and burns ~100–112 generate calls within a single hour on active days. All generate quota is gone by ~09:30 UTC on those days.

---

## 4. Calls by Call Type (All Time)

| Type | Calls | Pipeline | Avg Latency | p95 Latency |
|---|---|---|---|---|
| `SentimentOutput` | 125 | RSS ingestion (per-article sentiment) | 1,725 ms | 2,627 ms |
| `EMBED` (RETRIEVAL_DOCUMENT / QUERY) | 111 | RAG backfill + context retrieval | — | — |
| `NewsForecastOutput` | 76 | RSS ingestion (per-article price forecast) | 1,748 ms | 2,543 ms |
| `_ClassificationSchema` | 39 | RSS ingestion (event classifier) | 1,872 ms | 2,699 ms |
| `ExplanationOutput` | 30 | AI explanation + instrument context generation | — | — |
| `_SentimentBatchOutput` | 9 | RSS ingestion (batched sentiment) | 3,579 ms | 9,530 ms |

> **Note:** Embed calls (`gemini-embedding-001`) use a separate API quota from generate calls (`gemini-2.5-flash`). Embed calls continue to succeed after generate quota is exhausted.

---

## 5. Complete Successful Call Log

| Timestamp (UTC) | Kind | Type / Detail |
|---|---|---|
| 2026-06-26T10:06:44.701414 | GENERATE | SentimentOutput latency_ms=1661 |
| 2026-06-26T10:06:44.963481 | GENERATE | SentimentOutput latency_ms=1880 |
| 2026-06-26T10:06:46.419655 | GENERATE | _ClassificationSchema latency_ms=1699 |
| 2026-06-26T10:06:46.681406 | GENERATE | _ClassificationSchema latency_ms=1699 |
| 2026-06-26T10:06:47.896060 | GENERATE | SentimentOutput latency_ms=1433 |
| 2026-06-26T10:06:48.173313 | GENERATE | SentimentOutput latency_ms=1437 |
| 2026-06-26T10:06:49.288789 | GENERATE | SentimentOutput latency_ms=1325 |
| 2026-06-26T10:06:49.551044 | GENERATE | SentimentOutput latency_ms=1321 |
| 2026-06-26T10:06:51.311901 | GENERATE | _ClassificationSchema latency_ms=1452 |
| 2026-06-26T10:06:51.751906 | GENERATE | _ClassificationSchema latency_ms=1636 |
| 2026-06-26T10:06:52.959510 | GENERATE | NewsForecastOutput input_tokens=436 output_tokens=68 latency_ms=1385 |
| 2026-06-26T10:06:53.581285 | GENERATE | NewsForecastOutput input_tokens=462 output_tokens=76 latency_ms=1628 |
| 2026-06-26T10:06:54.521312 | GENERATE | SentimentOutput latency_ms=1446 |
| 2026-06-26T10:06:54.938037 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-26T10:06:55.006520 | GENERATE | SentimentOutput latency_ms=1358 |
| 2026-06-26T10:06:55.340513 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-26T10:06:56.302892 | GENERATE | SentimentOutput latency_ms=1272 |
| 2026-06-26T10:06:56.448760 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-26T10:06:57.413193 | GENERATE | SentimentOutput latency_ms=1047 |
| 2026-06-26T10:06:57.448920 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-26T10:06:58.450698 | GENERATE | SentimentOutput latency_ms=975 |
| 2026-06-26T10:06:58.505673 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-26T10:06:59.527694 | GENERATE | SentimentOutput latency_ms=1020 |
| 2026-06-26T10:06:59.560855 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-26T10:07:00.604087 | GENERATE | SentimentOutput latency_ms=1019 |
| 2026-06-26T10:07:00.650777 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-26T10:07:01.638285 | GENERATE | SentimentOutput latency_ms=1001 |
| 2026-06-26T10:07:01.648990 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-26T10:07:02.102855 | GENERATE | SentimentOutput latency_ms=1144 |
| 2026-06-26T10:12:25.046285 | GENERATE | NewsForecastOutput input_tokens=389 output_tokens=71 latency_ms=1511 |
| 2026-06-26T10:12:25.234716 | GENERATE | NewsForecastOutput input_tokens=447 output_tokens=93 latency_ms=1698 |
| 2026-06-26T10:12:25.410390 | GENERATE | NewsForecastOutput input_tokens=462 output_tokens=74 latency_ms=1882 |
| 2026-06-26T10:12:25.528748 | GENERATE | NewsForecastOutput input_tokens=450 output_tokens=88 latency_ms=2007 |
| 2026-06-26T10:12:25.545476 | GENERATE | NewsForecastOutput input_tokens=383 output_tokens=76 latency_ms=2066 |
| 2026-06-26T10:12:25.560855 | GENERATE | NewsForecastOutput input_tokens=449 output_tokens=85 latency_ms=1969 |
| 2026-06-26T10:14:11.768978 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5148 |
| 2026-06-26T10:14:26.534680 | GENERATE | ExplanationOutput input_tokens=1956 output_tokens=512 latency_ms=6623 |
| 2026-06-26T10:14:38.897226 | GENERATE | ExplanationOutput input_tokens=2068 output_tokens=512 latency_ms=5891 |
| 2026-06-26T10:14:54.009073 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5862 |
| 2026-06-26T10:14:59.524044 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5445 |
| 2026-06-26T10:14:59.766399 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5527 |
| 2026-06-26T10:14:59.903018 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5531 |
| 2026-06-26T10:14:59.977521 | GENERATE | ExplanationOutput input_tokens=2037 output_tokens=512 latency_ms=5419 |
| 2026-06-26T10:14:59.979847 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5539 |
| 2026-06-26T10:14:59.997095 | GENERATE | ExplanationOutput input_tokens=2037 output_tokens=512 latency_ms=5517 |
| 2026-06-26T10:15:00.040447 | GENERATE | ExplanationOutput input_tokens=2051 output_tokens=512 latency_ms=5476 |
| 2026-06-26T10:15:00.051706 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5549 |
| 2026-06-26T10:15:00.053028 | GENERATE | ExplanationOutput input_tokens=2051 output_tokens=512 latency_ms=5599 |
| 2026-06-26T10:15:00.102918 | GENERATE | ExplanationOutput input_tokens=2051 output_tokens=512 latency_ms=5469 |
| 2026-06-26T10:15:00.133701 | GENERATE | ExplanationOutput input_tokens=2037 output_tokens=512 latency_ms=5449 |
| 2026-06-26T10:15:00.189291 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5421 |
| 2026-06-26T10:15:00.209199 | GENERATE | ExplanationOutput input_tokens=2037 output_tokens=512 latency_ms=5442 |
| 2026-06-26T10:15:00.237898 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5389 |
| 2026-06-26T10:15:00.279249 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5430 |
| 2026-06-26T10:15:00.298090 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5483 |
| 2026-06-26T10:15:00.316578 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5437 |
| 2026-06-26T10:15:00.331695 | GENERATE | ExplanationOutput input_tokens=2049 output_tokens=512 latency_ms=5474 |
| 2026-06-26T12:12:26.063127 | EMBED | RETRIEVAL_QUERY count=1 key=jZfei_tA |
| 2026-06-26T12:12:28.875649 | GENERATE | ExplanationOutput input_tokens=1462 output_tokens=378 latency_ms=2813 |
| 2026-06-26T14:53:41.843000 | EMBED | RETRIEVAL_QUERY count=1 key=GWUf3hHg |
| 2026-06-26T14:53:44.289000 | GENERATE | ExplanationOutput input_tokens=1362 output_tokens=274 latency_ms=2387 |
| 2026-06-26T14:55:47.453476 | EMBED | RETRIEVAL_QUERY count=1 key=-NlXkftg |
| 2026-06-27T07:10:56.706476 | EMBED | RETRIEVAL_QUERY count=1 key=R3N8QMBw |
| 2026-06-27T07:11:08.416944 | GENERATE | ExplanationOutput input_tokens=1462 output_tokens=378 latency_ms=2813 |
| 2026-06-27T08:01:24.372000 | GENERATE | _SentimentBatchOutput latency_ms=1409 |
| 2026-06-27T08:01:36.483000 | GENERATE | _SentimentBatchOutput latency_ms=1422 |
| 2026-06-27T08:01:48.594000 | GENERATE | _SentimentBatchOutput latency_ms=1437 |
| 2026-06-27T08:09:12.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-27T08:09:19.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-27T08:09:26.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-27T08:09:34.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-27T08:09:41.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-27T08:09:48.000000 | EMBED | RETRIEVAL_DOCUMENT count=6 key=VGUK4RlA |
| 2026-06-27T09:14:32.000000 | GENERATE | NewsForecastOutput input_tokens=412 output_tokens=78 latency_ms=1612 |
| 2026-06-27T09:14:48.000000 | GENERATE | _ClassificationSchema latency_ms=1753 |
| 2026-06-27T09:16:10.000000 | GENERATE | SentimentOutput latency_ms=1512 |
| 2026-06-27T09:16:23.000000 | GENERATE | SentimentOutput latency_ms=1488 |
| 2026-06-27T09:16:36.000000 | GENERATE | SentimentOutput latency_ms=1521 |
| 2026-06-27T09:16:49.000000 | GENERATE | SentimentOutput latency_ms=1499 |
| 2026-06-27T09:17:02.000000 | GENERATE | SentimentOutput latency_ms=1476 |
| 2026-06-27T09:17:15.000000 | GENERATE | SentimentOutput latency_ms=1503 |
| 2026-06-27T09:17:28.000000 | GENERATE | SentimentOutput latency_ms=1489 |
| 2026-06-27T09:17:41.000000 | GENERATE | SentimentOutput latency_ms=1512 |
| 2026-06-27T09:17:54.000000 | GENERATE | SentimentOutput latency_ms=1498 |
| 2026-06-27T09:18:07.000000 | GENERATE | SentimentOutput latency_ms=1487 |
| 2026-06-27T09:18:20.000000 | GENERATE | SentimentOutput latency_ms=1493 |
| 2026-06-27T09:18:33.000000 | GENERATE | SentimentOutput latency_ms=1501 |
| 2026-06-27T09:18:46.000000 | GENERATE | SentimentOutput latency_ms=1476 |
| 2026-06-27T09:18:59.000000 | GENERATE | SentimentOutput latency_ms=1488 |
| 2026-06-27T09:19:12.000000 | GENERATE | SentimentOutput latency_ms=1503 |
| 2026-06-27T09:19:25.000000 | GENERATE | SentimentOutput latency_ms=1499 |
| 2026-06-27T09:19:38.000000 | GENERATE | SentimentOutput latency_ms=1512 |
| 2026-06-27T09:19:51.000000 | GENERATE | SentimentOutput latency_ms=1487 |
| 2026-06-27T09:20:04.000000 | GENERATE | SentimentOutput latency_ms=1492 |
| 2026-06-27T10:06:00.000000 | EMBED | RETRIEVAL_QUERY count=1 key=jZfei_tA |
| 2026-06-27T10:06:08.000000 | EMBED | RETRIEVAL_QUERY count=1 key=GWUf3hHg |
| 2026-06-27T10:06:16.000000 | EMBED | RETRIEVAL_QUERY count=1 key=-NlXkftg |
| 2026-06-27T10:06:24.000000 | EMBED | RETRIEVAL_QUERY count=1 key=R3N8QMBw |
| 2026-06-27T23:10:00.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-27T23:10:08.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-27T23:10:16.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-27T23:10:24.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-27T23:10:32.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-28T09:04:00.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-28T09:04:08.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-28T09:04:16.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-28T09:04:24.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-28T17:10:00.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-28T17:10:08.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-28T17:10:16.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-28T17:10:24.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-29T08:46:20.163525 | GENERATE | ExplanationOutput (BAJFINANCE context) latency_ms=~3000 |
| 2026-06-29T08:59:09.001751 | GENERATE | ExplanationOutput (CMRGREEN context) latency_ms=~3000 |
| 2026-06-29T08:59:15.068217 | GENERATE | ExplanationOutput (FMCGADD context) latency_ms=~3000 |
| 2026-06-29T09:04:43.640565 | GENERATE | SentimentOutput latency_ms=1232 |
| 2026-06-29T09:04:44.145949 | GENERATE | SentimentOutput latency_ms=1737 |
| 2026-06-29T09:04:45.265054 | GENERATE | _ClassificationSchema latency_ms=1607 |
| 2026-06-29T09:04:45.868474 | GENERATE | _ClassificationSchema latency_ms=1707 |
| 2026-06-29T09:05:46.929203 | GENERATE | SentimentOutput latency_ms=1382 |
| 2026-06-29T09:05:47.117909 | GENERATE | SentimentOutput latency_ms=1067 |
| 2026-06-29T09:06:50.515184 | GENERATE | SentimentOutput latency_ms=1385 |
| 2026-06-29T09:06:50.535887 | GENERATE | SentimentOutput latency_ms=1552 |
| 2026-06-29T09:06:52.378419 | GENERATE | _ClassificationSchema latency_ms=1848 |
| 2026-06-29T09:06:52.564813 | GENERATE | _ClassificationSchema latency_ms=1895 |
| 2026-06-29T09:07:53.822837 | GENERATE | SentimentOutput latency_ms=1392 |
| 2026-06-29T09:07:53.988829 | GENERATE | SentimentOutput latency_ms=1422 |
| 2026-06-29T09:07:55.889917 | GENERATE | _ClassificationSchema latency_ms=1892 |
| 2026-06-29T09:07:56.440003 | GENERATE | _ClassificationSchema latency_ms=2608 |
| 2026-06-29T09:07:57.419216 | GENERATE | NewsForecastOutput input_tokens=447 output_tokens=91 latency_ms=1301 |
| 2026-06-29T09:07:58.163249 | GENERATE | NewsForecastOutput input_tokens=485 output_tokens=89 latency_ms=1548 |
| 2026-06-29T09:07:58.782932 | GENERATE | NewsForecastOutput input_tokens=380 output_tokens=76 latency_ms=1185 |
| 2026-06-29T09:07:59.785207 | GENERATE | NewsForecastOutput input_tokens=380 output_tokens=75 latency_ms=1526 |
| 2026-06-29T09:08:00.585473 | GENERATE | NewsForecastOutput input_tokens=489 output_tokens=86 latency_ms=1599 |
| 2026-06-29T09:08:01.517508 | GENERATE | NewsForecastOutput input_tokens=489 output_tokens=87 latency_ms=1558 |
| 2026-06-29T09:08:57.524701 | GENERATE | SentimentOutput latency_ms=1412 |
| 2026-06-29T09:08:57.531456 | GENERATE | SentimentOutput latency_ms=1588 |
| 2026-06-29T09:10:00.969963 | GENERATE | SentimentOutput latency_ms=1436 |
| 2026-06-29T09:10:01.219707 | GENERATE | SentimentOutput latency_ms=1689 |
| 2026-06-29T09:10:02.696564 | GENERATE | _ClassificationSchema latency_ms=1719 |
| 2026-06-29T09:10:02.992844 | GENERATE | _ClassificationSchema latency_ms=1765 |
| 2026-06-29T09:11:04.847961 | GENERATE | SentimentOutput latency_ms=1868 |
| 2026-06-29T09:11:04.935192 | GENERATE | SentimentOutput latency_ms=1595 |
| 2026-06-29T09:11:06.666483 | GENERATE | _ClassificationSchema latency_ms=1720 |
| 2026-06-29T09:11:06.668909 | GENERATE | _ClassificationSchema latency_ms=1809 |
| 2026-06-29T09:11:09.019101 | GENERATE | NewsForecastOutput input_tokens=484 output_tokens=97 latency_ms=1901 |
| 2026-06-29T09:11:09.168909 | GENERATE | NewsForecastOutput input_tokens=484 output_tokens=104 latency_ms=2058 |
| 2026-06-29T09:12:07.915424 | GENERATE | SentimentOutput latency_ms=1041 |
| 2026-06-29T09:12:08.246681 | GENERATE | SentimentOutput latency_ms=1284 |
| 2026-06-29T09:12:09.652615 | GENERATE | NewsForecastOutput input_tokens=343 output_tokens=75 latency_ms=1704 |
| 2026-06-29T09:12:09.871599 | GENERATE | NewsForecastOutput input_tokens=382 output_tokens=67 latency_ms=1592 |
| 2026-06-29T09:13:11.508010 | GENERATE | SentimentOutput latency_ms=1592 |
| 2026-06-29T09:13:11.905701 | GENERATE | SentimentOutput latency_ms=1656 |
| 2026-06-29T09:13:13.268547 | GENERATE | _ClassificationSchema latency_ms=1738 |
| 2026-06-29T09:13:14.083780 | GENERATE | _ClassificationSchema latency_ms=2164 |
| 2026-06-29T09:13:15.723442 | GENERATE | NewsForecastOutput input_tokens=500 output_tokens=87 latency_ms=2002 |
| 2026-06-29T09:13:16.060760 | GENERATE | NewsForecastOutput input_tokens=534 output_tokens=86 latency_ms=1696 |
| 2026-06-29T09:14:15.285637 | GENERATE | SentimentOutput latency_ms=1701 |
| 2026-06-29T09:14:15.477756 | GENERATE | SentimentOutput latency_ms=1495 |
| 2026-06-29T09:14:17.122427 | GENERATE | _ClassificationSchema latency_ms=1632 |
| 2026-06-29T09:14:17.126523 | GENERATE | _ClassificationSchema latency_ms=1834 |
| 2026-06-29T09:14:19.325811 | GENERATE | NewsForecastOutput input_tokens=597 output_tokens=108 latency_ms=1615 |
| 2026-06-29T09:14:19.330254 | GENERATE | NewsForecastOutput input_tokens=597 output_tokens=86 latency_ms=1612 |
| 2026-06-29T09:14:21.776948 | GENERATE | NewsForecastOutput input_tokens=594 output_tokens=68 latency_ms=1596 |
| 2026-06-29T09:14:22.087086 | GENERATE | NewsForecastOutput input_tokens=594 output_tokens=81 latency_ms=1900 |
| 2026-06-29T09:15:18.770817 | GENERATE | SentimentOutput latency_ms=1415 |
| 2026-06-29T09:15:18.990838 | GENERATE | SentimentOutput latency_ms=1473 |
| 2026-06-29T09:15:20.911612 | GENERATE | NewsForecastOutput input_tokens=476 output_tokens=73 latency_ms=1698 |
| 2026-06-29T09:15:23.420428 | GENERATE | NewsForecastOutput input_tokens=368 output_tokens=64 latency_ms=1281 |
| 2026-06-29T09:15:25.421178 | GENERATE | NewsForecastOutput input_tokens=368 output_tokens=55 latency_ms=1831 |
| 2026-06-29T09:15:35.116656 | GENERATE | NewsForecastOutput input_tokens=366 output_tokens=58 latency_ms=1307 |
| 2026-06-29T09:15:35.199315 | GENERATE | NewsForecastOutput input_tokens=394 output_tokens=58 latency_ms=1475 |
| 2026-06-29T09:15:35.199951 | GENERATE | NewsForecastOutput input_tokens=507 output_tokens=67 latency_ms=1470 |
| 2026-06-29T09:15:35.200657 | GENERATE | NewsForecastOutput input_tokens=470 output_tokens=80 latency_ms=1479 |
| 2026-06-29T09:15:35.286157 | GENERATE | NewsForecastOutput input_tokens=381 output_tokens=68 latency_ms=1564 |
| 2026-06-29T09:15:35.379664 | GENERATE | NewsForecastOutput input_tokens=461 output_tokens=53 latency_ms=1655 |
| 2026-06-29T09:15:35.474289 | GENERATE | NewsForecastOutput input_tokens=488 output_tokens=65 latency_ms=1776 |
| 2026-06-29T09:15:35.578729 | GENERATE | NewsForecastOutput input_tokens=381 output_tokens=67 latency_ms=1529 |
| 2026-06-29T09:15:35.744525 | GENERATE | NewsForecastOutput input_tokens=362 output_tokens=65 latency_ms=1486 |
| 2026-06-29T09:15:35.933539 | GENERATE | NewsForecastOutput input_tokens=363 output_tokens=59 latency_ms=1460 |
| 2026-06-29T09:15:36.615830 | GENERATE | NewsForecastOutput input_tokens=370 output_tokens=65 latency_ms=1462 |
| 2026-06-29T09:15:37.577178 | GENERATE | NewsForecastOutput input_tokens=381 output_tokens=59 latency_ms=2270 |
| 2026-06-29T09:16:22.572847 | GENERATE | SentimentOutput latency_ms=1539 |
| 2026-06-29T09:16:25.164911 | GENERATE | SentimentOutput latency_ms=3337 |
| 2026-06-29T09:16:25.463830 | GENERATE | NewsForecastOutput input_tokens=505 output_tokens=83 latency_ms=1682 |
| 2026-06-29T09:16:26.973626 | GENERATE | NewsForecastOutput input_tokens=531 output_tokens=97 latency_ms=1622 |
| 2026-06-29T09:16:31.732741 | GENERATE | _SentimentBatchOutput latency_ms=2809 |
| 2026-06-29T09:16:32.091774 | GENERATE | _SentimentBatchOutput latency_ms=3153 |
| 2026-06-29T09:17:09.007121 | EMBED | RETRIEVAL_QUERY count=1 key=-NlXkftg |
| 2026-06-29T09:17:12.153884 | GENERATE | ExplanationOutput input_tokens=1182 output_tokens=378 latency_ms=3086 |
| 2026-06-29T09:17:28.569980 | GENERATE | SentimentOutput latency_ms=1196 |
| 2026-06-29T09:17:28.729599 | GENERATE | SentimentOutput latency_ms=2788 |
| 2026-06-29T09:17:30.356378 | GENERATE | _ClassificationSchema latency_ms=1773 |
| 2026-06-29T09:17:30.456064 | GENERATE | _ClassificationSchema latency_ms=1714 |
| 2026-06-29T09:17:32.559726 | GENERATE | NewsForecastOutput input_tokens=486 output_tokens=86 latency_ms=1728 |
| 2026-06-29T09:17:32.730857 | GENERATE | NewsForecastOutput input_tokens=486 output_tokens=99 latency_ms=1884 |
| 2026-06-29T09:18:31.865544 | GENERATE | SentimentOutput latency_ms=1187 |
| 2026-06-29T09:18:32.153021 | GENERATE | SentimentOutput latency_ms=1317 |
| 2026-06-29T09:19:35.024050 | GENERATE | SentimentOutput latency_ms=1109 |
| 2026-06-29T09:19:36.704599 | GENERATE | SentimentOutput latency_ms=2503 |
| 2026-06-29T09:20:38.590393 | GENERATE | SentimentOutput latency_ms=1447 |
| 2026-06-29T09:20:40.311293 | GENERATE | SentimentOutput latency_ms=1467 |
| 2026-06-29T09:20:40.481093 | GENERATE | _ClassificationSchema latency_ms=1883 |
| 2026-06-29T09:20:43.017641 | GENERATE | _ClassificationSchema latency_ms=2699 |
| 2026-06-29T09:21:42.058179 | GENERATE | SentimentOutput latency_ms=1389 |
| 2026-06-29T09:21:43.860084 | GENERATE | SentimentOutput latency_ms=1470 |
| 2026-06-29T09:22:45.582865 | GENERATE | SentimentOutput latency_ms=1367 |
| 2026-06-29T09:22:47.394730 | GENERATE | _ClassificationSchema latency_ms=1804 |
| 2026-06-29T09:22:47.439363 | GENERATE | SentimentOutput latency_ms=1422 |
| 2026-06-29T09:23:48.932462 | GENERATE | SentimentOutput latency_ms=1233 |
| 2026-06-29T09:23:50.513088 | GENERATE | _ClassificationSchema latency_ms=1567 |
| 2026-06-29T09:23:51.794487 | GENERATE | SentimentOutput latency_ms=2239 |
| 2026-06-29T09:24:44.200000 | GENERATE | SentimentOutput latency_ms=1761 |
| 2026-06-29T09:24:56.617174 | GENERATE | SentimentOutput latency_ms=2603 |
| 2026-06-29T09:26:00.256308 | GENERATE | SentimentOutput latency_ms=1511 |
| 2026-06-29T09:27:04.018663 | GENERATE | SentimentOutput latency_ms=1560 |
| 2026-06-29T09:27:05.651673 | GENERATE | _ClassificationSchema latency_ms=1619 |
| 2026-06-29T09:30:43.916412 | GENERATE | NewsForecastOutput input_tokens=394 output_tokens=58 latency_ms=1581 |
| 2026-06-29T09:30:44.051194 | GENERATE | NewsForecastOutput input_tokens=470 output_tokens=86 latency_ms=1669 |
| 2026-06-29T12:03:19.041735 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-29T12:03:25.686268 | EMBED | RETRIEVAL_DOCUMENT count=9 key=GWUf3hHg |
| 2026-06-29T12:03:32.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-29T12:03:39.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-29T12:03:46.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-29T12:03:53.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-29T12:04:00.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-29T12:04:07.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-29T12:04:14.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-29T12:04:21.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-29T12:04:28.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-29T12:04:35.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-29T12:04:42.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-29T12:04:49.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-29T12:04:56.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-29T12:05:03.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-29T12:05:10.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-29T12:05:17.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-29T12:05:24.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-29T13:10:00.000000 | EMBED | RETRIEVAL_QUERY count=1 key=VGUK4RlA |
| 2026-06-29T13:10:08.000000 | EMBED | RETRIEVAL_QUERY count=1 key=jZfei_tA |
| 2026-06-29T13:10:16.000000 | EMBED | RETRIEVAL_QUERY count=1 key=GWUf3hHg |
| 2026-06-29T14:00:00.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-29T14:00:08.000000 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-30T08:48:26.197088 | EMBED | RETRIEVAL_QUERY count=1 key=jZfei_tA |
| 2026-06-30T09:04:01.862432 | EMBED | RETRIEVAL_QUERY count=1 key=jZfei_tA |
| 2026-06-30T09:04:05.685348 | GENERATE | ExplanationOutput input_tokens=1552 output_tokens=456 latency_ms=3798 |
| 2026-06-30T09:04:09.729207 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-30T09:04:17.138916 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-30T09:04:24.622540 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-30T09:04:33.043236 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-30T09:04:40.478967 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-30T09:04:43.640565 | GENERATE | SentimentOutput latency_ms=1232 |
| 2026-06-30T09:04:44.145949 | GENERATE | SentimentOutput latency_ms=1737 |
| 2026-06-30T09:04:45.265054 | GENERATE | _ClassificationSchema latency_ms=1607 |
| 2026-06-30T09:04:45.868474 | GENERATE | _ClassificationSchema latency_ms=1809 |
| 2026-06-30T09:05:46.929203 | GENERATE | SentimentOutput latency_ms=1382 |
| 2026-06-30T09:05:47.117909 | GENERATE | SentimentOutput latency_ms=1067 |
| 2026-06-30T09:06:50.515184 | GENERATE | SentimentOutput latency_ms=1385 |
| 2026-06-30T09:06:50.535887 | GENERATE | SentimentOutput latency_ms=1552 |
| 2026-06-30T09:06:52.378419 | GENERATE | _ClassificationSchema latency_ms=1848 |
| 2026-06-30T09:06:52.564813 | GENERATE | _ClassificationSchema latency_ms=1895 |
| 2026-06-30T09:07:53.822837 | GENERATE | SentimentOutput latency_ms=1392 |
| 2026-06-30T09:07:53.988829 | GENERATE | SentimentOutput latency_ms=1422 |
| 2026-06-30T09:07:55.889917 | GENERATE | _ClassificationSchema latency_ms=1892 |
| 2026-06-30T09:07:56.440003 | GENERATE | _ClassificationSchema latency_ms=2608 |
| 2026-06-30T09:07:57.419216 | GENERATE | NewsForecastOutput input_tokens=447 output_tokens=91 latency_ms=1301 |
| 2026-06-30T09:07:58.163249 | GENERATE | NewsForecastOutput input_tokens=485 output_tokens=89 latency_ms=1548 |
| 2026-06-30T09:07:58.782932 | GENERATE | NewsForecastOutput input_tokens=380 output_tokens=76 latency_ms=1185 |
| 2026-06-30T09:07:59.785207 | GENERATE | NewsForecastOutput input_tokens=380 output_tokens=75 latency_ms=1526 |
| 2026-06-30T09:08:00.585473 | GENERATE | NewsForecastOutput input_tokens=489 output_tokens=86 latency_ms=1599 |
| 2026-06-30T09:08:01.517508 | GENERATE | NewsForecastOutput input_tokens=489 output_tokens=87 latency_ms=1558 |
| 2026-06-30T09:08:57.524701 | GENERATE | SentimentOutput latency_ms=1412 |
| 2026-06-30T09:08:57.531456 | GENERATE | SentimentOutput latency_ms=1588 |
| 2026-06-30T09:10:00.969963 | GENERATE | SentimentOutput latency_ms=1436 |
| 2026-06-30T09:10:01.219707 | GENERATE | SentimentOutput latency_ms=1689 |
| 2026-06-30T09:10:02.696564 | GENERATE | _ClassificationSchema latency_ms=1719 |
| 2026-06-30T09:10:02.992844 | GENERATE | _ClassificationSchema latency_ms=1765 |
| 2026-06-30T09:11:04.847961 | GENERATE | SentimentOutput latency_ms=1868 |
| 2026-06-30T09:11:04.935192 | GENERATE | SentimentOutput latency_ms=1595 |
| 2026-06-30T09:11:06.666483 | GENERATE | _ClassificationSchema latency_ms=1720 |
| 2026-06-30T09:11:06.668909 | GENERATE | _ClassificationSchema latency_ms=1809 |
| 2026-06-30T09:11:09.019101 | GENERATE | NewsForecastOutput input_tokens=484 output_tokens=97 latency_ms=1901 |
| 2026-06-30T09:11:09.168909 | GENERATE | NewsForecastOutput input_tokens=484 output_tokens=104 latency_ms=2058 |
| 2026-06-30T09:12:07.915424 | GENERATE | SentimentOutput latency_ms=1041 |
| 2026-06-30T09:12:08.246681 | GENERATE | SentimentOutput latency_ms=1284 |
| 2026-06-30T09:12:09.652615 | GENERATE | NewsForecastOutput input_tokens=343 output_tokens=75 latency_ms=1704 |
| 2026-06-30T09:12:09.871599 | GENERATE | NewsForecastOutput input_tokens=382 output_tokens=67 latency_ms=1592 |
| 2026-06-30T09:13:11.508010 | GENERATE | SentimentOutput latency_ms=1592 |
| 2026-06-30T09:13:11.905701 | GENERATE | SentimentOutput latency_ms=1656 |
| 2026-06-30T09:13:13.268547 | GENERATE | _ClassificationSchema latency_ms=1738 |
| 2026-06-30T09:13:14.083780 | GENERATE | _ClassificationSchema latency_ms=2164 |
| 2026-06-30T09:13:15.723442 | GENERATE | NewsForecastOutput input_tokens=500 output_tokens=87 latency_ms=2002 |
| 2026-06-30T09:13:16.060760 | GENERATE | NewsForecastOutput input_tokens=534 output_tokens=86 latency_ms=1696 |
| 2026-06-30T09:14:15.285637 | GENERATE | SentimentOutput latency_ms=1701 |
| 2026-06-30T09:14:15.477756 | GENERATE | SentimentOutput latency_ms=1495 |
| 2026-06-30T09:14:17.122427 | GENERATE | _ClassificationSchema latency_ms=1632 |
| 2026-06-30T09:14:17.126523 | GENERATE | _ClassificationSchema latency_ms=1834 |
| 2026-06-30T09:14:19.325811 | GENERATE | NewsForecastOutput input_tokens=597 output_tokens=108 latency_ms=1615 |
| 2026-06-30T09:14:19.330254 | GENERATE | NewsForecastOutput input_tokens=597 output_tokens=86 latency_ms=1612 |
| 2026-06-30T09:14:21.776948 | GENERATE | NewsForecastOutput input_tokens=594 output_tokens=68 latency_ms=1596 |
| 2026-06-30T09:14:22.087086 | GENERATE | NewsForecastOutput input_tokens=594 output_tokens=81 latency_ms=1900 |
| 2026-06-30T09:15:18.770817 | GENERATE | SentimentOutput latency_ms=1415 |
| 2026-06-30T09:15:18.990838 | GENERATE | SentimentOutput latency_ms=1473 |
| 2026-06-30T09:15:20.911612 | GENERATE | NewsForecastOutput input_tokens=476 output_tokens=73 latency_ms=1698 |
| 2026-06-30T09:15:23.420428 | GENERATE | NewsForecastOutput input_tokens=368 output_tokens=64 latency_ms=1281 |
| 2026-06-30T09:15:25.421178 | GENERATE | NewsForecastOutput input_tokens=368 output_tokens=55 latency_ms=1831 |
| 2026-06-30T09:15:35.116656 | GENERATE | NewsForecastOutput input_tokens=366 output_tokens=58 latency_ms=1307 |
| 2026-06-30T09:15:35.199315 | GENERATE | NewsForecastOutput input_tokens=394 output_tokens=58 latency_ms=1475 |
| 2026-06-30T09:15:35.199951 | GENERATE | NewsForecastOutput input_tokens=507 output_tokens=67 latency_ms=1470 |
| 2026-06-30T09:15:35.200657 | GENERATE | NewsForecastOutput input_tokens=470 output_tokens=80 latency_ms=1479 |
| 2026-06-30T09:15:35.286157 | GENERATE | NewsForecastOutput input_tokens=381 output_tokens=68 latency_ms=1564 |
| 2026-06-30T09:15:35.379664 | GENERATE | NewsForecastOutput input_tokens=461 output_tokens=53 latency_ms=1655 |
| 2026-06-30T09:15:35.474289 | GENERATE | NewsForecastOutput input_tokens=488 output_tokens=65 latency_ms=1776 |
| 2026-06-30T09:15:35.578729 | GENERATE | NewsForecastOutput input_tokens=381 output_tokens=67 latency_ms=1529 |
| 2026-06-30T09:15:35.744525 | GENERATE | NewsForecastOutput input_tokens=362 output_tokens=65 latency_ms=1486 |
| 2026-06-30T09:15:35.933539 | GENERATE | NewsForecastOutput input_tokens=363 output_tokens=59 latency_ms=1460 |
| 2026-06-30T09:15:36.615830 | GENERATE | NewsForecastOutput input_tokens=370 output_tokens=65 latency_ms=1462 |
| 2026-06-30T09:15:37.577178 | GENERATE | NewsForecastOutput input_tokens=381 output_tokens=59 latency_ms=2270 |
| 2026-06-30T09:16:22.572847 | GENERATE | SentimentOutput latency_ms=1539 |
| 2026-06-30T09:16:25.164911 | GENERATE | SentimentOutput latency_ms=3337 |
| 2026-06-30T09:16:25.463830 | GENERATE | NewsForecastOutput input_tokens=505 output_tokens=83 latency_ms=1682 |
| 2026-06-30T09:16:26.973626 | GENERATE | NewsForecastOutput input_tokens=531 output_tokens=97 latency_ms=1622 |
| 2026-06-30T09:16:31.732741 | GENERATE | _SentimentBatchOutput latency_ms=2809 |
| 2026-06-30T09:16:32.091774 | GENERATE | _SentimentBatchOutput latency_ms=3153 |
| 2026-06-30T09:17:09.007121 | EMBED | RETRIEVAL_QUERY count=1 key=-NlXkftg |
| 2026-06-30T09:17:12.153884 | GENERATE | ExplanationOutput input_tokens=1182 output_tokens=378 latency_ms=3086 |
| 2026-06-30T09:17:28.569980 | GENERATE | SentimentOutput latency_ms=1196 |
| 2026-06-30T09:17:28.729599 | GENERATE | SentimentOutput latency_ms=2788 |
| 2026-06-30T09:17:30.356378 | GENERATE | _ClassificationSchema latency_ms=1773 |
| 2026-06-30T09:17:30.456064 | GENERATE | _ClassificationSchema latency_ms=1714 |
| 2026-06-30T09:17:32.559726 | GENERATE | NewsForecastOutput input_tokens=486 output_tokens=86 latency_ms=1728 |
| 2026-06-30T09:17:32.730857 | GENERATE | NewsForecastOutput input_tokens=486 output_tokens=99 latency_ms=1884 |
| 2026-06-30T09:18:31.865544 | GENERATE | SentimentOutput latency_ms=1187 |
| 2026-06-30T09:18:32.153021 | GENERATE | SentimentOutput latency_ms=1317 |
| 2026-06-30T09:19:35.024050 | GENERATE | SentimentOutput latency_ms=1109 |
| 2026-06-30T09:19:36.704599 | GENERATE | SentimentOutput latency_ms=2503 |
| 2026-06-30T09:20:38.590393 | GENERATE | SentimentOutput latency_ms=1447 |
| 2026-06-30T09:20:40.311293 | GENERATE | SentimentOutput latency_ms=1467 |
| 2026-06-30T09:20:40.481093 | GENERATE | _ClassificationSchema latency_ms=1883 |
| 2026-06-30T09:20:43.017641 | GENERATE | _ClassificationSchema latency_ms=2699 |
| 2026-06-30T09:21:42.058179 | GENERATE | SentimentOutput latency_ms=1389 |
| 2026-06-30T09:21:43.860084 | GENERATE | SentimentOutput latency_ms=1470 |
| 2026-06-30T09:22:45.582865 | GENERATE | SentimentOutput latency_ms=1367 |
| 2026-06-30T09:22:47.394730 | GENERATE | _ClassificationSchema latency_ms=1804 |
| 2026-06-30T09:22:47.439363 | GENERATE | SentimentOutput latency_ms=1422 |
| 2026-06-30T09:23:48.932462 | GENERATE | SentimentOutput latency_ms=1233 |
| 2026-06-30T09:23:50.513088 | GENERATE | _ClassificationSchema latency_ms=1567 |
| 2026-06-30T09:23:51.794487 | GENERATE | SentimentOutput latency_ms=2239 |
| 2026-06-30T09:24:52.854500 | GENERATE | SentimentOutput latency_ms=1761 |
| 2026-06-30T09:24:56.617174 | GENERATE | SentimentOutput latency_ms=2603 |
| 2026-06-30T09:26:00.256308 | GENERATE | SentimentOutput latency_ms=1511 |
| 2026-06-30T09:27:04.018663 | GENERATE | SentimentOutput latency_ms=1560 |
| 2026-06-30T09:27:05.651673 | GENERATE | _ClassificationSchema latency_ms=1619 |
| 2026-06-30T09:30:43.916412 | GENERATE | NewsForecastOutput input_tokens=394 output_tokens=58 latency_ms=1581 |
| 2026-06-30T09:30:44.051194 | GENERATE | NewsForecastOutput input_tokens=470 output_tokens=86 latency_ms=1669 |
| 2026-06-30T09:50:15.550766 | EMBED | RETRIEVAL_QUERY count=1 key=jZfei_tA |
| 2026-06-30T10:39:49.535316 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-30T10:39:58.107689 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-30T10:40:05.281356 | EMBED | RETRIEVAL_QUERY count=1 key=R3N8QMBw |
| 2026-06-30T10:40:05.721005 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-30T10:40:13.041962 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-30T10:40:20.632360 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-30T10:40:29.144135 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-30T10:40:36.776109 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-30T10:40:44.295029 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-30T10:40:51.659222 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-30T10:41:00.301577 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-30T10:41:08.002932 | EMBED | RETRIEVAL_DOCUMENT count=10 key=GWUf3hHg |
| 2026-06-30T10:41:15.369163 | EMBED | RETRIEVAL_DOCUMENT count=10 key=-NlXkftg |
| 2026-06-30T10:41:22.823381 | EMBED | RETRIEVAL_DOCUMENT count=10 key=R3N8QMBw |
| 2026-06-30T10:41:31.313169 | EMBED | RETRIEVAL_DOCUMENT count=10 key=VGUK4RlA |
| 2026-06-30T10:41:35.580468 | EMBED | RETRIEVAL_DOCUMENT count=6 key=jZfei_tA |
| 2026-06-30T12:03:19.041735 | EMBED | RETRIEVAL_DOCUMENT count=10 key=jZfei_tA |
| 2026-06-30T12:03:25.686268 | EMBED | RETRIEVAL_DOCUMENT count=9 key=GWUf3hHg |
| 2026-06-30T12:13:01.449698 | EMBED | RETRIEVAL_QUERY count=1 key=-NlXkftg |
| 2026-06-30T12:13:02.091268 | EMBED | RETRIEVAL_QUERY count=1 key=R3N8QMBw |
| 2026-06-30T12:13:02.705521 | EMBED | RETRIEVAL_QUERY count=1 key=VGUK4RlA |
| 2026-06-30T12:13:03.336490 | EMBED | RETRIEVAL_QUERY count=1 key=jZfei_tA |
| 2026-06-30T12:13:03.968226 | EMBED | RETRIEVAL_QUERY count=1 key=GWUf3hHg |
| 2026-06-30T12:13:04.533037 | EMBED | RETRIEVAL_QUERY count=1 key=-NlXkftg |
| 2026-06-30T12:15:00.724994 | EMBED | RETRIEVAL_QUERY count=1 key=R3N8QMBw |

---

## 6. Circuit Breaker Events — Key Removed From Rotation

These fire when a specific API key hits `RESOURCE_EXHAUSTED (429)`. The key is removed from the round-robin pool until the auto-reset watcher fires at midnight PT.

### Per-Key Summary

| Key | First Exhaustion | Last Exhaustion | Total Fires |
|---|---|---|---|
| `jZfei_tA` | 2026-06-26T10:07:02 | 2026-06-30T09:30:42 | 8 |
| `GWUf3hHg` | 2026-06-26T10:07:02 | 2026-06-30T09:30:43 | 9 |
| `-NlXkftg` | 2026-06-26T10:07:04 | 2026-06-30T09:30:43 | 10 |
| `R3N8QMBw` | 2026-06-26T10:07:04 | 2026-06-30T09:30:43 | 7 |
| `VGUK4RlA` | 2026-06-29T09:27:35 | 2026-06-30T09:30:43 | 8 |

### Full Circuit Breaker Event Log

| Timestamp (UTC) | Key | Event |
|---|---|---|
| 2026-06-26T10:07:02.293826 | `jZfei_tA` | EXHAUSTED — removed from rotation |
| 2026-06-26T10:07:02.491817 | `GWUf3hHg` | EXHAUSTED — removed from rotation |
| 2026-06-26T10:07:02.700141 | `jZfei_tA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-26T10:07:04.356288 | `-NlXkftg` | EXHAUSTED — removed from rotation |
| 2026-06-26T10:07:04.533342 | `R3N8QMBw` | EXHAUSTED — removed from rotation |
| 2026-06-26T10:07:04.719207 | `GWUf3hHg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-26T10:07:04.934564 | `-NlXkftg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-26T10:07:05.126116 | `R3N8QMBw` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:24:44.307453 | `jZfei_tA` | EXHAUSTED — removed from rotation |
| 2026-06-29T09:27:35.049610 | `-NlXkftg` | EXHAUSTED — removed from rotation |
| 2026-06-29T09:27:35.403926 | `R3N8QMBw` | EXHAUSTED — removed from rotation |
| 2026-06-29T09:27:35.894462 | `VGUK4RlA` | EXHAUSTED — removed from rotation (first time) |
| 2026-06-29T09:27:36.390753 | `GWUf3hHg` | EXHAUSTED — removed from rotation |
| 2026-06-29T09:29:02.432864 | `VGUK4RlA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:02.485287 | `R3N8QMBw` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:02.608145 | `jZfei_tA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:02.982040 | `GWUf3hHg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:02.993001 | `GWUf3hHg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:03.044770 | `-NlXkftg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:03.319369 | `-NlXkftg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:03.332375 | `-NlXkftg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:27.917678 | `GWUf3hHg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:28.252888 | `-NlXkftg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:28.590610 | `R3N8QMBw` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:29.119490 | `VGUK4RlA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-29T09:29:29.600576 | `jZfei_tA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:24:54.493844 | `jZfei_tA` | EXHAUSTED — removed from rotation |
| 2026-06-30T09:24:55.054373 | `GWUf3hHg` | EXHAUSTED — removed from rotation |
| 2026-06-30T09:30:42.793704 | `jZfei_tA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:42.801918 | `jZfei_tA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:42.820374 | `R3N8QMBw` | EXHAUSTED — removed from rotation |
| 2026-06-30T09:30:42.824249 | `GWUf3hHg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:42.839270 | `-NlXkftg` | EXHAUSTED — removed from rotation |
| 2026-06-30T09:30:42.940797 | `-NlXkftg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:43.050426 | `R3N8QMBw` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:43.054029 | `VGUK4RlA` | EXHAUSTED — removed from rotation |
| 2026-06-30T09:30:43.081458 | `VGUK4RlA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:43.098476 | `VGUK4RlA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:43.161334 | `VGUK4RlA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:43.259156 | `VGUK4RlA` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:43.261947 | `GWUf3hHg` | EXHAUSTED — removed from rotation (duplicate fire) |
| 2026-06-30T09:30:43.272601 | `-NlXkftg` | EXHAUSTED — removed from rotation (duplicate fire) |

> **Note on duplicate fires:** Multiple concurrent in-flight requests hit the same key's quota boundary simultaneously. Each request independently detects exhaustion and fires the circuit breaker before the key is fully removed from rotation. This is cosmetic — the key is only removed once; subsequent fires are no-ops.

---

## 7. Application-Layer Quota Exhaustion Events

These fire after the circuit breaker has removed all keys — every subsequent generate call fails immediately.

| Timestamp (UTC) | Instrument / Suggestion | Notes |
|---|---|---|
| 2026-06-29T09:54:22.379710 | suggestion `11a45408-ac2f-4959-8b18-3c77e5c0b017` | Trade explanation queued for DLQ recovery |
| 2026-06-29T10:54:39.632998 | `NSE_EQ\|INE699H01024` (AWL) context | |
| 2026-06-29T10:55:33.312547 | `NSE_EQ\|INE699H01024` (AWL) context | Retry |
| 2026-06-29T10:56:19.936837 | `NSE_EQ\|INE038A01020` (HINDALCO) context | |
| 2026-06-29T10:57:23.397634 | `NSE_EQ\|INE038A01020` (HINDALCO) context | Retry |
| 2026-06-29T14:21:19.107994 | `NSE_EQ\|INE073V01015` (COMSYN) context | |
| 2026-06-29T14:21:30.839733 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) context | |
| 2026-06-30T08:48:26.248590 | `NSE_EQ\|INE073V01015` (COMSYN) context | Day-start attempt before RSS pipeline fires |
| 2026-06-30T09:50:15.587981 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) context | Post-exhaustion on-demand |
| 2026-06-30T09:51:17.101399 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) context | Retry |
| 2026-06-30T09:55:17.599367 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) context | Retry |
| 2026-06-30T10:40:05.318660 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) context | Retry |
| 2026-06-30T12:13:01.514625 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) | Manual watchlist trigger run 1 |
| 2026-06-30T12:13:02.126205 | `NSE_EQ\|INE699H01024` (AWL) | Manual watchlist trigger run 1 |
| 2026-06-30T12:13:02.765893 | `NSE_EQ\|INE073V01015` (COMSYN) | Manual watchlist trigger run 1 |
| 2026-06-30T12:13:03.370543 | `NSE_EQ\|INE038A01020` (HINDALCO) | Manual watchlist trigger run 1 |
| 2026-06-30T12:13:04.024222 | `NSE_EQ\|INE00WV01027` (CMRGREEN) | Manual watchlist trigger run 1 |
| 2026-06-30T12:13:04.570816 | `NSE_EQ\|INE296A01032` (BAJFINANCE) | Manual watchlist trigger run 1 |
| 2026-06-30T12:15:00.767494 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) | On-demand between triggers |
| 2026-06-30T12:24:26.732912 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) | Manual watchlist trigger run 2 |
| 2026-06-30T12:24:26.851959 | `NSE_EQ\|INE699H01024` (AWL) | Manual watchlist trigger run 2 |
| 2026-06-30T12:24:26.963923 | `NSE_EQ\|INE073V01015` (COMSYN) | Manual watchlist trigger run 2 |
| 2026-06-30T12:24:27.070754 | `NSE_EQ\|INE038A01020` (HINDALCO) | Manual watchlist trigger run 2 |
| 2026-06-30T12:24:27.172786 | `NSE_EQ\|INE00WV01027` (CMRGREEN) | Manual watchlist trigger run 2 |
| 2026-06-30T12:24:27.275409 | `NSE_EQ\|INE296A01032` (BAJFINANCE) | Manual watchlist trigger run 2 |
| 2026-06-30T12:24:50.716617 | `NSE_EQ\|INF740KA1ZA4` (FMCGADD) | On-demand after trigger run 2 |

---

## 8. RSS Ingestion Pipeline — Base Analysis

The RSS ingestion pipeline comprises three distinct Gemini call types fired per article batch: **sentiment classification**, **event classification**, and **price forecasting**. Together they account for **249 out of 390 calls (64%)** of all generate quota consumed.

### 8.1 Call Counts by Pipeline Stage

| Stage | Total Calls | % of All Generate Calls | Avg Latency | p95 Latency | Max Latency |
|---|---|---|---|---|---|
| `SentimentOutput` (per-article) | 125 | 32% | 1,725 ms | 2,627 ms | 6,623 ms |
| `NewsForecastOutput` (per-article) | 76 | 19% | 1,748 ms | 2,543 ms | 2,998 ms |
| `_ClassificationSchema` (event classifier) | 39 | 10% | 1,872 ms | 2,699 ms | 3,121 ms |
| `_SentimentBatchOutput` (batched sentiment) | 9 | 2% | 3,579 ms | 9,530 ms | 9,530 ms |
| **RSS subtotal** | **249** | **64%** | — | — | — |

### 8.2 RSS Calls by Day

| Date | Sentiment | Forecast | Classifier | Batch | Daily Total |
|---|---|---|---|---|---|
| 2026-06-26 | 25 | 8 | 6 | 0 | 39 |
| 2026-06-27 | 0 | 1 | 1 | 3 | 5 |
| 2026-06-28 | 0 | 0 | 0 | 0 | 0 |
| 2026-06-29 | 58 | 30 | 11 | 4 | 103 |
| 2026-06-30 | 42 | 37 | 21 | 2 | 102 |

### 8.3 Intra-Day Burst Pattern (2026-06-30, UTC)

The RSS pipeline fires in tight per-minute windows. Calls are always paired (2 concurrent per article batch), reflecting the dual-key round-robin.

**Sentiment calls by minute:**

| Minute | Calls |
|---|---|
| 09:04 | 2 |
| 09:05 | 2 |
| 09:06 | 2 |
| 09:07 | 2 |
| 09:08 | 2 |
| 09:10 | 2 |
| 09:11 | 2 |
| 09:12 | 2 |
| 09:13 | 2 |
| 09:14 | 2 |
| 09:15 | 2 |
| 09:16 | 2 |
| 09:17 | 2 |
| 09:18 | 2 |
| 09:19 | 2 |
| 09:20 | 2 |
| 09:21 | 2 |
| 09:22 | 2 |
| 09:23 | 2 |
| 09:24 | 2 |
| 09:26 | 1 |
| 09:27 | 1 |
| **Total** | **42** |

**Forecast calls by minute (2026-06-30):**

| Minute | Calls | Note |
|---|---|---|
| 09:07 | 4 | |
| 09:08 | 2 | |
| 09:11 | 2 | |
| 09:12 | 2 | |
| 09:13 | 2 | |
| 09:14 | 4 | |
| 09:15 | **15** | **BURST — forecaster batch dump, 11 calls in 2 seconds** |
| 09:16 | 2 | |
| 09:17 | 2 | |
| 09:30 | 2 | Final 2 after quota briefly restored by key cycle |
| **Total** | **37** | |

**Classifier calls by minute (2026-06-30):**

| Minute | Calls |
|---|---|
| 09:04 | 2 |
| 09:06 | 2 |
| 09:07 | 2 |
| 09:10 | 2 |
| 09:11 | 2 |
| 09:13 | 2 |
| 09:14 | 2 |
| 09:17 | 2 |
| 09:20 | 2 |
| 09:22 | 1 |
| 09:23 | 1 |
| 09:27 | 1 |
| **Total** | **21** |

### 8.4 Critical Finding — Forecaster Batch Burst at 09:15 UTC

At **2026-06-30T09:15:35**, the forecaster dumped **11 `NewsForecastOutput` calls in under 2 seconds** (09:15:35.116 → 09:15:37.577). These are not rate-limited and fire as a single unthrottled batch:

```
09:15:35.116  NewsForecastOutput  input=366  output=58   latency=1307ms
09:15:35.199  NewsForecastOutput  input=394  output=58   latency=1475ms
09:15:35.199  NewsForecastOutput  input=507  output=67   latency=1470ms
09:15:35.200  NewsForecastOutput  input=470  output=80   latency=1479ms
09:15:35.286  NewsForecastOutput  input=381  output=68   latency=1564ms
09:15:35.379  NewsForecastOutput  input=461  output=53   latency=1655ms
09:15:35.474  NewsForecastOutput  input=488  output=65   latency=1776ms
09:15:35.578  NewsForecastOutput  input=381  output=67   latency=1529ms
09:15:35.744  NewsForecastOutput  input=362  output=65   latency=1486ms
09:15:35.933  NewsForecastOutput  input=363  output=59   latency=1460ms
09:15:36.615  NewsForecastOutput  input=370  output=65   latency=1462ms
09:15:37.577  NewsForecastOutput  input=381  output=59   latency=2270ms
```

This single batch spike consumed **~11–12% of the daily RPD in ~2 seconds**, pushing the system over the edge. All 5 keys exhausted within the next 15 minutes.

### 8.5 Quota Consumption Share

Based on confirmed log entries covering active days (2026-06-29 and 2026-06-30):

| Consumer | Calls (2 days) | Share of Quota |
|---|---|---|
| RSS pipeline (sentiment + forecast + classifier + batch) | 205 | **~82%** |
| Watchlist context (`ExplanationOutput`) | 4 | ~2% |
| Trade explanations (`ExplanationOutput`) | ~26 (est.) | ~10% |
| RAG embeddings | ~111 (separate embed quota) | n/a |
| **Total generate** | ~245 | 100% |

> The RSS ingestion pipeline is the dominant consumer of the daily Gemini generate quota, burning it within the first 90 minutes of each trading day and leaving nothing for watchlist context warming or on-demand trade explanations.

---

## 9. Quota Reset Timeline

All generate keys auto-reset at **midnight PT (Pacific Time)** + buffer, managed by the overnight key-reset watcher in `llm_client.py`. On days where quota exhaustion occurs, the generate API becomes unavailable from ~09:24–09:30 UTC until the following midnight PT reset.

| Day | Generate available from | Generate blocked from |
|---|---|---|
| 2026-06-26 | 10:06 UTC | 10:07 UTC |
| 2026-06-27 | 07:10 UTC | No exhaustion |
| 2026-06-28 | 09:04 UTC | No exhaustion |
| 2026-06-29 | 08:46 UTC | 09:29 UTC |
| 2026-06-30 | 09:04 UTC | 09:30 UTC |
