# Shared Fragment: Severity Triage Output (🔴🟡🟢)

> Referenced by review units whose report uses severity triage. Header wording, item id
> shape, per-item fields, and verdict tokens stay unit-defined; localize labels to the
> selected output language.

```
## 📋 {unit-defined header}

**Target**: (unit-defined)
**Summary**: (1–2 sentences)

---

### 🔴 Must-fix issues
Per item: **{item id}** — problem + unit-defined fields (why it matters, suggested fix, …)
(If none: "No issues found ✅")

---

### 🟡 Suggested improvements
Same item shape. (If none: same ✅ line)

---

### 🟢 What is already solid
- Name the good parts and good patterns concretely.
```

State explicitly when a section has no findings. Findings must be actionable and
evidence-backed; when uncertain, say the behavior may be intentional and needs
confirmation.
