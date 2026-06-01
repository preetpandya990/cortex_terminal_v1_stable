# 08 — Serving & Infrastructure
> Date: 2026-06-01 | Covers: inference engine, quantization, latency/throughput, cost, hardware

**Over 90% of total LLM operational cost is inference** — so serving efficiency, not training, dominates the long-run bill. A 10x inference improvement compounds across every request, session, and agentic loop.

---

## 1. Inference engine

**vLLM** is the production default in 2026 (PagedAttention + continuous batching):
- ~**1,850 tokens/sec** at 50 concurrent requests on Llama-3.3-70B at FP8.
- **2–4x** throughput over naive serving; **200+ requests/min** on a single GPU for smaller models.
- **TTFT ~120 ms** (H100, 50 concurrent) — competitive with TensorRT-LLM (~105 ms).

Alternatives: **TensorRT-LLM** (lowest latency on NVIDIA, more setup), **TGI** (HF-native). Default to vLLM unless a latency benchmark says otherwise.

---

## 2. Quantization — the single biggest lever

> "Quantization is the single biggest optimization you can apply before touching your serving engine."

- **FP8** cuts VRAM **50–75%** and lifts throughput by relieving memory-bandwidth limits.
- Stacked: **FP8 + FlashAttention-3 + continuous batching + speculative decoding** ≈ **5–8x** better cost-efficiency vs naive FP16 + static batching on an H100.
- Validate quality post-quantization against the eval harness ([`07`](./07-evaluation-and-quality.md)) — quantization can shift outputs; gate it.

> **Decision: serve quantized (FP8) by default; prove quality parity on the eval harness before promoting any quantized build.**

---

## 3. Latency budgets by class

| Class | Target | Levers |
|-------|--------|--------|
| Interactive analyst | TTFT < ~150 ms, sub-second full | 7B–14B, FP8, spec decoding, warm pool |
| Batch signals/reports | Throughput-optimized | Larger model OK, max batch, off-peak |

Match model size ([`03`](./03-architecture-and-models.md)) to the class — don't pay 70B latency for an interactive path a 14B handles.

---

## 4. Hardware & cost (2026)

- **Hopper (H100/H200)** — current workhorse for 7B–70B serving.
- **Blackwell (GB300 NVL72)** — up to **50x** low-latency perf and **~35x lower cost/token** vs Hopper for the heaviest workloads; ~10x cost/token reductions reported by major providers. Relevant only at large scale.
- **Start rented/cloud, on-demand.** Buy/commit only when sustained utilization justifies it. This keeps the "resources undefined" path open and cheap.

---

## 5. Observability

Track per the 2026 standard: **TTFT, inter-token latency, tokens/sec, cost/request, GPU utilization, KV-cache hit rate, queue depth.** Wire into the existing **Prometheus + Grafana** stack (you already run it) so LLM serving sits beside the rest of Cortex's telemetry.

> **Decision: serving SLOs (latency, cost/req, error rate) are defined up front and dashboarded from day one — not retrofitted.**
