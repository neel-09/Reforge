# Model Catalog & Telemetry Specification (`model_catalog.py`)

## 1. Nebius Token Factory Pricing Catalog
| Tier Key | Model ID | Input Rate ($ / 1M tokens) | Output Rate ($ / 1M tokens) | Role / Usage Share |
| :--- | :--- | :--- | :--- | :--- |
| `lightning` | `nvidia/Nemotron-3.5-Lightning` | $0.06 | $0.24 | Primary code patcher (~80%) |
| `super` | `nvidia/Nemotron-3.5-Super-VL` | $0.30 | $0.90 | Self-healing diagnosis (~15%) |
| `ultra` | `nvidia/Nemotron-3-Ultra` | $1.00 | $3.00 | Refactoring planner (~5%) |

---

## 2. Telemetry & Cost Formulas
* **Actual Step Cost:**
  $$\text{Cost}_{\text{step}} = \left(\frac{\text{Tokens}_{\text{in}}}{10^6} \times R_{\text{tier, in}}\right) + \left(\frac{\text{Tokens}_{\text{out}}}{10^6} \times R_{\text{tier, out}}\right)$$
* **Counterfactual 100% Ultra Cost:**
  $$\text{Cost}_{\text{ultra}} = \left(\frac{\text{Tokens}_{\text{in}}}{10^6} \times 1.00\right) + \left(\frac{\text{Tokens}_{\text{out}}}{10^6} \times 3.00\right)$$
* **Dollar Savings:**
  $$\text{Savings} = \text{Cost}_{\text{ultra}} - \text{Cost}_{\text{step}}$$

---

## 3. Data Structure Contract
Every LLM dispatch logs an entry adhering to this schema:

```python
{
    "timestamp": "2026-10-25T12:00:00Z",
    "tier": "lightning",
    "model": "nvidia/Nemotron-3.5-Lightning",
    "prompt_tokens": 1250,
    "completion_tokens": 320,
    "latency_ms": 420.5,
    "actual_cost_usd": 0.0001518,
    "counterfactual_ultra_cost_usd": 0.0022100,
    "savings_usd": 0.0020582,
    "first_pass_success": True
}