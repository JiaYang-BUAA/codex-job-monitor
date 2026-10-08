# Codex Job Monitor

监视本地或 SSH Windows 计算作业，在进程退出、明确失败或到达终态时，
向指定的 Codex Desktop 聊天发送一条简短状态消息。

基于实际使用的计算事件监视器整理。Python 标准库实现，不需要安装 pip 依赖。
监视期间不调用模型；事件入队后，Codex 才可能开始处理并使用对应账户额度。

## 工作方式

```text
作业状态文件 + PID/创建时间
              ↓
      本地 PowerShell / 只读 SSH 探针
              ↓
      终态判定 → 持久化事件与证据
              ↓
      聊天 idle 且原生队列为空？
          否：等待    是：入队一次
```

- 支持单个本地作业、单个远程作业以及明确列出的多个远程作业。
- 以进程 PID 加创建时间识别一次运行，避免 PID 重用误判。
- 不以低 CPU、日志不增长或文件时间变化判断计算完成。
- 无定时唤醒、两小时提醒，也不停止或重启计算。
- 配置与运行证据留在本地；发送结果未知时不自动重试。

## 环境要求

- 监视端：Windows、Python 3.11+、PowerShell 7（`pwsh`）。
- 通知端：本机 Codex Desktop 已启动，并提供仅本机可访问的 CDP 调试端口。
- SSH 模式：本机 OpenSSH 客户端、已配置的 SSH 主机别名和密钥；远端为 Windows，支持 Windows PowerShell 5.1+。
- 作业包装器按约定写入状态和进程身份，见[证据格式](docs/evidence-format.md)。

**兼容性边界：**通知通过 Codex Desktop 的内部接口完成，不属于稳定公开 API。
Desktop 更新可能导致接口失效。仅安装 Codex 并不保证 CDP 可用；
请先用 `doctor` 验证。如果客户端未开启调试端口，需要通过该客户端支持的启动方式启用，
例如适用时使用 `--remote-debugging-port=9335`，然后重启客户端。
该启动参数是否可用取决于你的客户端版本和安装方式，本项目不自动更改它。
调试端口只应绑定回环地址，不能暴露到局域网或公网。

## 快速开始

下载并解压仓库，或克隆后进入目录，在 PowerShell 中执行：

```powershell
python monitor.py init
```

编辑生成的 `config.json`，将这些字段替换为自己的值：

| 字段 | 说明 |
|---|---|
| `thread_id` | 接收通知的现有 Codex 聊天 ID，不是聊天标题 |
| `cwd` | 该聊天的工作目录，绝对路径 |
| `cdp_url` | 本机 CDP 地址，如 `http://127.0.0.1:9335` |
| `runtime_dir` | 本地监视状态目录；相对路径以配置文件所在目录为基准 |
| `poll_seconds` | 轮询间隔，默认 15 秒 |
| `pwsh` | PowerShell 7 命令或可执行文件路径 |
| `target` | 明确指定的本地或远程运行 |

聊天 ID 可从当前客户端提供的聊天链接或开发工具中取得；不要把聊天分享链接当作 ID。
每个独立监视实例应使用自己的配置和 `runtime_dir`。

先进行只读检查：

```powershell
python monitor.py doctor
python monitor.py probe
```

`doctor` 检查依赖、Desktop、目标聊天及队列读取接口，`probe` 读取指定作业证据；两者均不入队消息。
确认配置后，显式启用通知：

```powershell
python monitor.py run --enable-send
```

终端运行期间保持开启。停止监视（不会停止计算作业）：

```powershell
python monitor.py stop
```

使用不同配置时，将全局参数放在子命令前：

```powershell
python monitor.py --config .\my-config.json probe
python monitor.py --config .\my-config.json run --enable-send
```

## 本地演示

打开另一个 PowerShell 窗口运行一个只等待 60 秒的示例：

```powershell
pwsh -NoProfile -File .\examples\demo-local-job.ps1 -JobDirectory E:\Jobs\demo-001 -Seconds 60
```

把配置中的 `target` 设为：

```json
{"mode":"local","job_directory":"E:\\Jobs\\demo-001"}
```

用 `probe` 查看从 running 到 program_completed 的变化。
只有 `run --enable-send` 会向配置的聊天发送通知。
每次演示使用新目录，例如 `demo-002`；脚本拒绝覆盖已有目录。

## SSH 与多个作业

参考 `config.remote.example.json` 的配置，并将 `probe_remote_target.ps1`
预先放入远程作业目录内，路径与 `probe_file` 一致。
监视器不会帮你复制文件或启动远程计算。

多个远程目标的 `target` 结构为：

```json
{"mode":"remote-list","targets":["这里替换为完整远程 target 对象", "另一个远程 target 对象"]}
```

上面仅示意结构，字符串不能直接作为目标。各目标必须拥有独立 `job_id`。
每轮全部目标探测通过结构和身份校验后才处理事件；任何目标连接失败时暂缓该轮投递。
某一目标返回 `valid:false` 则跳过该目标，不将其误认为停止。

## 运行状态与问题排查

`runtime_dir` 内保存 `status.json`、`run.json`、`job-observation.json`、
`job-state.json` 以及 `job-events/<event_id>/` 下的证据和交付回执。

| 情况 | 处理 |
|---|---|
| CDP 不可达 | 检查 Desktop 是否启动、调试端口及客户端兼容性；重新执行 doctor |
| 作业 `valid:false` | 根据 error 检查证据格式、文件权限和进程身份 |
| 聊天忙或队列非空 | 等待；不会强行插入活动回合 |
| `delivery_needs_review` / `outcome_unknown_no_retry` | 核对事件回执、聊天历史和队列，保留状态供人工处理 |
| 已入队但未看到回复 | 入队回执不是模型完成回执；检查目标聊天和客户端队列 |
| 同一作业不再通知 | 检查是否复用了旧 run ID 或目录；同一次终态只通知一次 |

不要删除整个状态目录来解决投递结果未知的问题，这可能造成重复唤醒。
通知只报告状态，不代表计算收敛或科学验收通过。

## 开发与测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖事件判定、目标约束、未知结果防重试、队列保护和 PowerShell 探针。
自动测试不发送真实聊天消息。GitHub Actions 在 Windows 上运行测试。
开发测试额外使用 Node.js 执行隔离的 JavaScript 队列保护测试；监视器运行无需 Node.js。

## 许可与来源

MIT，见 [LICENSE](LICENSE)。Desktop 传输层的来源见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
这是独立项目，不是 OpenAI 官方产品。
