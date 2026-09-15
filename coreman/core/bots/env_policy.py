"""运行时控制类环境变量黑名单：机器人 env、技能预设与技能用户配置共用。

这些 env 最终原样并入 CLI 进程环境，而 CLI 以免审批模式运行。机器人管理员若能设置
下面的变量，就能不经模型、确定性地做到两件事：把整轮上下文（提示词、用户消息、附件、
发言者令牌）导向自己的「模型」，或者在 runtime 主机上直接执行代码。所以这里只拦
「改变 CLI/解释器/加载器行为」的变量，普通业务配置（DB_PASSWORD、ERP_BASE_URL 等）照常放行。

runtime_daemon/daemon.py 自带一份同样的规则（守护进程是独立包，不能 import coreman），
tests/unit/test_env_policy.py 保证两份逐项一致；改这里必须同步改那里。

刻意没有拦的：
- 通用的 `*_BASE_URL` 后缀：技能普遍用 `XXX_BASE_URL` 指向业务系统；模型端点都带厂商前缀
  （ANTHROPIC_BASE_URL、ANTHROPIC_BEDROCK_BASE_URL、OPENAI_BASE_URL、CODEX_OSS_BASE_URL），
  已由前缀覆盖；切换 Bedrock/Vertex 需要 CLAUDE_CODE_USE_*，同样被拦。
- PIP_* / UV_* 等包索引变量：只有模型主动执行安装时才生效，和免审批模式下模型本来就能
  执行的命令同级，拦了也挡不住，反而误伤技能。
"""

from __future__ import annotations

from collections.abc import Iterable

BLOCKED_ENV_PREFIXES: tuple[str, ...] = (
    # 模型端点、API 凭据与 CLI 配置：ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN、
    # OPENAI_BASE_URL、CLAUDE_CONFIG_DIR / CLAUDE_CODE_USE_BEDROCK、CODEX_HOME 等。
    "ANTHROPIC_",
    "OPENAI_",
    "CLAUDE_",
    "CODEX_",
    # 动态链接器预加载（Linux / macOS）：LD_PRELOAD、LD_LIBRARY_PATH、DYLD_INSERT_LIBRARIES。
    "LD_",
    "DYLD_",
    # 解释器与工具链启动时加载代码：PYTHONPATH / PYTHONSTARTUP / PYTHONWARNINGS、
    # npm 的 script-shell / registry、.NET CoreCLR profiler、OpenSSL 引擎与配置。
    "PYTHON",
    "NPM_CONFIG_",
    "CORECLR_",
    "OPENSSL_",
    # bash 从环境导入的函数定义（BASH_FUNC_name%%）。
    "BASH_FUNC_",
    # Git / SSH 可执行钩子：GIT_SSH_COMMAND、GIT_ASKPASS、GIT_CONFIG_*（可设 core.fsmonitor 等）、
    # GIT_EXEC_PATH、SSH_ASKPASS、SSH_AUTH_SOCK（借用节点自己的 SSH agent）。
    "GIT_",
    "SSH_",
    # TLS 信任：SSL_CERT_FILE / SSL_CERT_DIR。CA 由节点服务环境统一配置。
    "SSL_",
    # 配置目录：git、ripgrep 等从 XDG_CONFIG_HOME 读配置，配置里可以挂可执行钩子。
    "XDG_",
)

BLOCKED_ENV_KEYS: frozenset[str] = frozenset(
    {
        # 用户环境：HOME 决定 CLI 配置与凭据目录，PATH 决定执行哪个二进制，SHELL 是
        # Claude Code 执行 Bash 工具用的解释器，TMPDIR 里存放会被 source 的 shell 快照。
        "HOME",
        "PATH",
        "SHELL",
        "TMPDIR",
        # 嵌套会话探测标记，Go 驱动启动 CLI 前会主动去掉它。
        "CLAUDECODE",
        # 解释器启动选项。
        "NODE_OPTIONS",
        "NODE_PATH",
        "BUN_OPTIONS",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "PERL5DB",
        "RUBYOPT",
        "RUBYLIB",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
        "DOTNET_STARTUP_HOOKS",
        "GOFLAGS",
        # glibc iconv 按 GCONV_PATH 加载共享库，任何 glibc 程序都可能触发。
        "GCONV_PATH",
        # shell 启动与提示符求值：非交互 bash 读 BASH_ENV，sh 读 ENV；SHELLOPTS=xtrace 配合
        # PS4 里的命令替换即可执行；zsh 从 ZDOTDIR 读启动文件。
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "PROMPT_COMMAND",
        "ZDOTDIR",
        # 子程序会调起的外部命令：编辑器、分页器、浏览器、less 预处理器、sudo 口令程序、
        # ripgrep 配置（--pre 可指定任意预处理命令）。
        "EDITOR",
        "VISUAL",
        "PAGER",
        "MANPAGER",
        "BROWSER",
        "LESSOPEN",
        "LESSCLOSE",
        "SUDO_ASKPASS",
        "RIPGREP_CONFIG_PATH",
        # TLS 信任与校验开关。
        "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        # 出站代理由节点配置（config.json 的 proxy）统一设置。按机器人覆盖会绕开运维的出口
        # 策略，并让代理方看到全部出站连接；配合任何 CA 变量即可解密，一并拦下。
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "FTP_PROXY",
        # runtime 节点自身的身份令牌。
        "COREMAN_AGENT_TOKEN",
        "COREMAN_NODE_TOKEN",
        "COREMAN_INSTALL_TOKEN",
    }
)

# 前缀规则里的例外：只影响提交署名，不改变执行行为。
ALLOWED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        # 厂商 API key 与模型名：技能（如图片生成、自带 LLM 客户端）直接读取。只换 key 不改变请求
        # 发往的端点，端点类（*_BASE_URL）仍被前缀拦下；CLI 的模型由驱动 --model 显式指定。
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
    }
)


def is_blocked_env_key(key: str) -> bool:
    # 按大写比较：API 只接受大写键，但 curl 等程序也认小写的 http_proxy。
    name = key.upper()
    if name in ALLOWED_ENV_KEYS:
        return False
    return name in BLOCKED_ENV_KEYS or name.startswith(BLOCKED_ENV_PREFIXES)


def blocked_env_keys(keys: Iterable[str]) -> list[str]:
    """返回命中黑名单的键（排序去重），只含键名，不碰值。"""
    return sorted({key for key in keys if is_blocked_env_key(key)})
