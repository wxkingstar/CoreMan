"""调度员知识库：每天把白名单来源抽成事实条目，再增量编译进调度员工作目录的 Wiki。

见 docs/superpowers/specs/2026-11-28-dispatcher-knowledge-base.md。分工：

- `sources`：读哪些来源、排除什么，由代码决定；
- `extract`：抽取回合的提示词与结果解析；
- `filters`：条目入库前、Wiki 写完后的规则过滤；
- `wiki`：编译回合的提示词与编译后检查；
- `rounds`：在承接员工所在的节点上选后端、跑一轮；
- `runner`：把上面串成一次运行，记进度与运行记录。
"""
