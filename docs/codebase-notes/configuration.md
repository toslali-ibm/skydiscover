# Configuration Reference

## Config Resolution Order

1. Load YAML from `--config` (or defaults)
2. Apply CLI overrides (`--model`, `--search`, `--iterations`, etc.)
3. Resolve API keys from environment variables
4. Post-init: fill in per-model defaults, expand env vars

## Config Dataclass Hierarchy

All defined in `skydiscover/config.py` (~800 lines):

```
Config
  |- language: str              # "python", "text", "image", etc.
  |- diff_based_generation: bool
  |- max_iterations: int
  |- file_suffix: str
  |
  |- llm: LLMConfig
  |    |- models: List[LLMModelConfig]   # Weighted pool
  |    |- api_base: str
  |    |- temperature: float = 0.7
  |    |- max_tokens: int = 32000
  |
  |- search: SearchConfig
  |    |- type: str                      # "adaevolve", "evox", "topk", etc.
  |    |- num_context_programs: int
  |    |- database: DatabaseConfig       # Algorithm-specific subconfig
  |
  |- context_builder: ContextBuilderConfig
  |    |- template: str
  |    |- system_message: str
  |    |- evaluator_system_message: str
  |
  |- evaluator: EvaluatorConfig
  |    |- timeout: int
  |    |- cascade_evaluation: bool
  |    |- cascade_thresholds: List[float]
  |    |- use_llm_feedback: bool
  |
  |- agentic: AgenticConfig
  |    |- enabled: bool
  |    |- codebase_root: str
  |    |- max_steps: int
  |
  |- monitor: MonitorConfig
  |    |- enabled: bool
  |    |- host: str
  |    |- port: int
  |
  |- human_feedback_enabled: bool
```

## Available Config Templates

Located in `configs/`:

| File | Algorithm | Use Case |
|------|-----------|----------|
| `default.yaml` | TopK | Minimal starting template |
| `adaevolve.yaml` | AdaEvolve | Full multi-island config |
| `evox.yaml` | EvoX | Co-evolution config |
| `openevolve_native.yaml` | OpenEvolve Native | MAP-Elites + islands |
| `llm_judge.yaml` | Any | LLM-as-judge demo |
| `human_in_the_loop.yaml` | TopK | Monitor + human feedback |

## Full Parameter Reference

See the [configs/README.md](../configs/README.md) for the complete annotated reference of every parameter, including:
- Top-level settings (iterations, checkpoints, language, diff mode)
- LLM settings (models, temperature, tokens, timeout, multi-model pools)
- Search settings per algorithm (AdaEvolve islands/migration/paradigm, EvoX co-evolution, Beam Search width/diversity)
- Prompt settings (system message, evaluator message)
- Evaluator settings (timeout, cascade, LLM judge)
- Agentic mode settings
- Monitor settings
- Human feedback settings

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI API key (default provider) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Gemini API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `MISTRAL_API_KEY` | Mistral API key |
| `OPENAI_API_BASE` / `OPENAI_BASE_URL` | Override API endpoint globally |

## Model Provider Format

Any LiteLLM-compatible model using `provider/model` syntax:

```
gpt-5                              # OpenAI (default)
gemini/gemini-3-pro-preview         # Gemini
anthropic/claude-sonnet-4-6         # Anthropic
deepseek/deepseek-chat              # DeepSeek
ollama/llama3                       # Local (Ollama)
vllm/my-model                       # Local (vLLM)
```
