# Sources
> Date: 2026-06-01 | Web research underpinning this KB. Grouped by topic.

Sources are starting points, not gospel — verify licenses, costs, and benchmark claims against primary docs before committing budget. Costs and benchmark numbers move fast.

---

## Approach: from-scratch vs fine-tune vs RAG (incl. BloombergGPT vs FinGPT)
- [FinGPT: Open-Source Financial LLMs (arXiv 2306.06031)](https://arxiv.org/html/2306.06031v2)
- [FinGPT vs BloombergGPT — Unite.AI](https://www.unite.ai/generative-ai-in-finance-fingpt-bloomberggpt-beyond/)
- [FinLoRA: Benchmarking LoRA on financial datasets (arXiv 2505.19819)](https://arxiv.org/html/2505.19819v1)
- [IBM — RAG vs Fine-tuning](https://www.ibm.com/think/topics/rag-vs-fine-tuning)
- [CFA Institute — Practical Guide for LLMs in the Financial Industry](https://rpc.cfainstitute.org/research/the-automation-ahead-content-series/practical-guide-for-llms-in-the-financial-industry)

## Base models, fine-tuning, data volume
- [Fine-tuning an LLM for Financial Analysis — real lessons (Medium)](https://medium.com/@rafa.souza.pinto/fine-tuning-an-llm-for-financial-analysis-three-attempts-real-lessons-c9eec7c3e605)
- [LLMs Meet Finance: Fine-Tuning for the Open FinLLM Leaderboard (arXiv 2504.13125)](https://arxiv.org/pdf/2504.13125)
- [How Much Data to Fine-Tune an LLM in 2026 — Particula](https://particula.tech/blog/how-much-data-fine-tune-llm)
- [Best Open-Source LLMs 2026 — Hugging Face](https://huggingface.co/blog/daya-shankar/open-source-llms)

## Reasoning models (Fin-R1 / Fin-o1 / GRPO distillation)
- [Fin-R1: Financial Reasoning via RL (arXiv 2503.16252)](https://arxiv.org/html/2503.16252v5)
- [Fin-o1: Transferability of Reasoning-Enhanced LLMs to Finance (arXiv 2502.08127)](https://arxiv.org/html/2502.08127v3)
- [DeepSeek-R1 Distilled Models Overview — EmergentMind](https://www.emergentmind.com/topics/deepseek-r1-distilled-models)

## Domain-adaptive continued pretraining & forgetting
- [Demystifying Domain-adaptive Post-training for Financial LLMs (arXiv 2501.04961)](https://arxiv.org/html/2501.04961v1)
- [IKnow: Instruction-Knowledge-Aware Continual Pretraining (arXiv 2510.20377)](https://arxiv.org/pdf/2510.20377)
- [MortgageLLM: Domain-Adaptive Pretraining w/ Residual Instruction Transfer (arXiv 2511.21101)](https://arxiv.org/html/2511.21101v1)

## Data curation & synthetic data
- [DCLM-Baseline — EmergentMind](https://www.emergentmind.com/topics/dclm-baseline-dataset)
- [NVIDIA NeMo Curator — Domain-Adaptive Pretraining](https://developer.nvidia.com/blog/streamlining-data-processing-for-domain-adaptive-pretraining-with-nvidia-nemo-curator/)
- [Synthetic Data for LLM Fine-Tuning in 2026 — FutureAGI](https://futureagi.com/blog/synthetic-data-fine-tuning-llms/)
- [Post-Training in 2026: GRPO, DAPO, RLVR & Beyond — llm-stats](https://llm-stats.com/blog/research/post-training-techniques-2026)
- [Synthetic Instruction Datasets for Japanese Financial Domain (arXiv 2603.01353)](https://arxiv.org/pdf/2603.01353)

## RAG for finance
- [Architecting Alpha: RAG in Quant Finance — Sophie AI](https://www.sophie-ai-finance.com/articles/architecting-alpha-rag-evolution-quantitative-finance)
- [RAG for Finance: Beyond the Tutorial — FinTech Studios](https://www.fintechstudios.com/blog/rag-pipelines-financial-intelligence-best-practices)
- [RAG for Finance: Context-Aware Retrieval — Statement](https://www.askstatement.com/blog/rag-for-finance-how-to-build-context-aware-retrieval-systems/)
- [Enterprise financial data analysis assistant w/ LangChain — DEV](https://dev.to/jamesli/build-an-enterprise-level-financial-data-analysis-assistant-multi-source-data-rag-system-practice-2c2h)

## Evaluation benchmarks
- [FinBen: Holistic Financial Benchmark — ACM](https://dl.acm.org/doi/10.5555/3737916.3740949)
- [FINESSE-Bench (arXiv 2605.15482)](https://arxiv.org/abs/2605.15482)
- [FinMMEval Lab @ CLEF 2026 — Springer](https://link.springer.com/chapter/10.1007/978-3-032-21321-1_37)
- [Finance LLM Leaderboard 2026 — AwesomeAgents](https://awesomeagents.ai/leaderboards/finance-llm-leaderboard/)

## Serving & inference
- [How to Use vLLM for Production [2026] — Markaicode](https://markaicode.com/tutorial/how-to-use-vllm/)
- [LLM Inference Optimization Playbook — Runpod](https://www.runpod.io/articles/guides/llm-inference-optimization-playbook)
- [LLM Inference Optimization & Quantization 2026 — Zylos](https://zylos.ai/research/2026-01-15-llm-inference-optimization)
- [vLLM vs TGI performance study (arXiv 2511.17593)](https://arxiv.org/pdf/2511.17593)

## Agentic finance
- [TradingAgents: Multi-Agent LLM Trading Framework — GitHub](https://github.com/TauricResearch/TradingAgents)
- [Automating Financial Signal Discovery w/ Multi-Agent Systems — NVIDIA](https://developer.nvidia.com/blog/automating-and-optimizing-financial-signal-discovery-with-multi-agent-systems/)
- [MountainLion: Multi-Modal LLM Agent for Trading (arXiv 2507.20474)](https://arxiv.org/pdf/2507.20474)

## Governance & compliance
- [LLM Guardrails for Fintech — Maxim AI](https://www.getmaxim.ai/articles/llm-guardrails-for-fintech-compliance-hallucination-prevention-and-audit-trails/)
- [SR 11-7 & AI Governance — The Algo](https://www.the-algo.com/insights/ai-governance-financial-services-sr1107)
- [GRC Roles for LLM Deployment in Financial Services — Neurons Lab](https://neurons-lab.com/governance-risk-compliance-roles-for-llm-deployment-financial-services/)
- [SR 11-7 Model Risk Management — ModelOp](https://www.modelop.com/ai-governance/ai-regulations-standards/sr-11-7)

## Scaling laws & fundamentals
- [Cameron Wolfe — LLM Scaling Laws](https://cameronrwolfe.substack.com/p/llm-scaling-laws)
- [Scaling Laws for LLM Pretraining — jonvet](https://www.jonvet.com/blog/llm-scaling-laws)
- [Sebastian Raschka — Pre-/Post-training Paradigms](https://sebastianraschka.com/blog/2024/new-llm-pre-training-and-post-training.html)
