## 一、ADR 怎么写

ADR 就是**放在代码仓库里的一个短 markdown 文件**，每个重要决策一份。最常用的格式来自 Michael Nygard，只有五个部分：

```markdown
# ADR 0003: Store money as NUMERIC, not float

## Status
Accepted (2026-11-10)

## Context
We store fee amounts in Postgres. Clients will check them against
their own records, so they must be exact to the cent.

## Decision
Use NUMERIC(19, 4) for all money columns, and Python's Decimal in code.

## Alternatives considered
- FLOAT / DOUBLE: fast, but 0.1 + 0.2 != 0.3. Rounding errors build up
  across many rows. Rejected.
- Integer cents (BIGINT): exact and fast, but breaks for currencies
  with 3 decimals and fees with fractional cents. Rejected.

## Consequences
- Slightly slower arithmetic, which doesn't matter at our scale.
- Every developer and the AI agent must use Decimal, never float, for money.
```

**怎么落地：**

1. **放在仓库里。**统一放在 `docs/adr/`，按编号命名，比如 `0001-use-postgres.md`、`0002-audit-events-bigserial.md`。这样它跟代码一起版本管理，code review 时也能一起看。
2. **只给难以回滚的决策写。**比如数据库选型、主键类型、金额怎么存、认证方式、要不要引入某个框架。改个函数名不需要。经验法则：**以后改起来要做数据迁移或大范围重构的，就值得写一份。**
3. **决策变了不要改旧的，写一份新的。**新 ADR 写明"取代 ADR 0003"，旧的状态改成 Superseded。这样决策的历史就保留下来了。

**跟 Claude Code 怎么配合：**

- **让 AI 起草选项，你做决定。**比如说："I need to decide how to store money in Postgres. Draft an ADR with 2–3 options and their trade-offs, but don't pick one." 你读完，自己做选择，再让它把 Decision 部分补上。
- **让 AI 遵守已有的决策。**在 `CLAUDE.md` 里加一行："Read docs/adr/ before making design changes. Follow accepted ADRs. If a change conflicts with one, stop and ask me." 这样 agent 就不会悄悄推翻你的架构决策。

**用你的 auditable-rag demo 练手：**可以为这几个决策各写一份 ADR：
- 为什么审计表用 append-only 加哈希链。
- 为什么 `seq` 用 BIGSERIAL 而不是 SERIAL。
- 为什么原文单独存在 blob 表里（GDPR）。

面试时能把这几份 ADR 拿出来讲，非常有说服力。

## 二、"PR 带证据"怎么做

核心是**一个 PR 模板**，再加上 **CI 自动产出证据**。

**1. 加一个 PR 模板。**在仓库里建 `.github/pull_request_template.md`。GitHub 每次开 PR 时会自动填进描述框：

```markdown
## What and why
<!-- One or two sentences. Link the issue or spec. -->

## Risk tier
- [ ] Low (docs, styling, copy)
- [ ] Medium (normal feature, business logic)
- [ ] High (auth, permissions, money, data schema, audit log)

## AI involvement
<!-- e.g. "Implementation drafted by Claude Code; tests written by me;
     hash-chain logic reviewed line by line." -->

## Evidence it works
- [ ] Tests pass (CI link or pasted output)
- [ ] Manually checked: <!-- what you did and what you saw -->
- [ ] Screenshot / log snippet, if there is UI or runtime behaviour

## Please focus on
<!-- 1–2 places where you most want a human's eyes. -->

## Related ADRs
<!-- e.g. ADR 0002 -->
```

**2. 让 CI 自动产出证据，不要手动截图。**用 GitHub Actions，在每个 PR 上自动跑测试、lint 和覆盖率，结果直接显示在 PR 页面上。这样"证据"就不依赖自觉了。

**3. 一个填好的例子**，以你的 demo 为例：

> **What and why:** Add `verify_chain.py`, which recomputes the audit hash chain and reports the first broken event.
> **Risk tier:** High (audit log integrity)
> **AI involvement:** Claude Code drafted the script. I wrote the tamper tests myself, and reviewed the hashing logic line by line.
> **Evidence:** CI green, 12 tests passing. Ran `tamper_demo.py` locally: the verifier flags seq 42 and exits with code 1.
> **Please focus on:** the canonical JSON function, since a mismatch there would break every hash.

**4. 跟 Claude Code 怎么配合：**让它根据模板起草 PR 描述，但"Evidence"和"Please focus on"这两部分**你要亲自确认或者自己写**。特别是 Evidence：要你真的看到测试通过、真的跑过 demo，而不是 AI 说它跑过了。