# 作业证据格式

每次启动计算使用新的目录或新的 `job_id`。不要在旧标识下覆盖一次新运行：
监视器按标识去重，复用标识会漏掉新运行的完成事件。
`start_ticks` 是 PowerShell 的 `.StartTime.ToUniversalTime().Ticks`，不是 Unix 时间戳。
启动脚本应及时记录所有受监视进程，使用临时文件加原子重命名写入 JSON。

## 本地 Windows 作业

目标目录必须包含 `wrapper-status.json`：

```json
{"state":"running","started_utc":"2026-01-01T00:00:00Z"}
```

`owned-processes.json` 是数组，包含此次运行的所有所属进程：

```json
[{"pid":12345,"start_ticks":639028224000000000}]
```

以上 PID 和 ticks 只是格式示例，不能用于真实监视。
进程身份必须在启动时读取；仅有 PID 会受到 PID 重用影响。

程序结束后，包装器可写入：

```json
{"state":"program_completed","started_utc":"2026-01-01T00:00:00Z","finished_utc":"2026-01-01T01:00:00Z","child_exit_code":0,"wrapper_exit_code":0}
```

可选 `output/summary.json`：`{"status":"FINISHED"}`。
缺少包装器记录、JSON 损坏或进程访问失败会产生无效观测，不会被当作进程退出。
运行 `examples/demo-local-job.ps1` 可生成完整的本地示例；它只等待，不执行计算。

## 通过 SSH 监视 Windows 作业

将仓库的 `probe_remote_target.ps1` 放在目标目录内，由配置中的 `probe_file` 指定相对位置。
监视器运行期间只读证据文件和进程信息，不上传脚本，也不修改远程作业。
使用已配置好的 OpenSSH 主机别名与密钥；`BatchMode=yes` 不会弹出密码提示。
首次连接的主机密钥应事先由你验证。

远程探针读取的文件由 target 配置逐项指定，相对路径必须位于 `job_directory` 内：

| 字段 | 内容 |
|---|---|
| `launch_file` | 必需；启动进程的 `pid`、`start_ticks`、ISO 8601 `started_utc` |
| `owned_file` | `{"guard_pid":12345,"processes":[{"pid":12345,"start_ticks":639028224000000000}]}` |
| `summary_file` | 可选；包含字符串 `status`，可另含科学验收数据 |
| `driver_exit_file` | 可选；`pid` 应匹配 launch，另有整数 `exit_code` 与 ISO 8601 `exited_utc` |

远程 `owned_file` 是对象；本地版本是数组。两者不可互换。
远程探针同时检查 launch 进程和 owned 列表，不能只因为主驱动返回 0 就宣称所有子进程退出。
未提供 owned 列表时，进程集合未知，不产生“全部进程退出”事件。

## 事件与交付

- 所有已确认的所属进程身份消失，或读到明确的终态/失败记录，产生一个待发事件。
- 常规进度、CPU 使用率、文件修改时间不会触发通知；没有周期提醒。
- 明确失败可能发生在仍有进程运行时，因此“记录明确失败”与“进程已退出”是不同通知。
- 程序停止与科学验收通过是不同事实；监视器不替代结果审查。
- 目标聊天忙、状态未知或队列非空时继续等待；投递前在同一连接内检查。
- 入队回执仅表示消息已排队，不证明模型已处理。
- 提交结果不确定时保留证据并停止重试，需要人工核对；不能通过删除状态文件强行重发。

首次启动时，已处于终态的配置作业也会产生事件。只在准备好通知时启动 `run`。
