# Roadmap / 待办

三件事,按依赖排序。**顺序 A → B → C 是有依赖的**:清空是重跑的地基,重跑是 UI 的验收基准。

状态(2026-10-23):**A、B 已完成**;**C 功能与自动化验收完成**,真实全量计时验收在当轮主机条件下中止、待空闲主机复测(见 C 节末尾的实测与复测方法)。

---

## A. 一键清空管线资产(`pipeline-clean`)— ✅ 已完成(2026-10-22)

交付物:`bin/pipeline-clean`(三级)、`tests/ci/run-clean-check.sh`(50+ 条断言,CI job
`cleanup-tool`,0.4 s)、README 的「Cleaning up — three levels, one command」小节、设计文档
F41 / D25、磁盘预检提示语改为指向本工具(并加了一条断言钉住它)。

### 实际的分级

| 级别 | 删什么 | 保留什么 | 下次成本 |
|---|---|---|---|
| `check`(**默认**,删 0 字节) | 无;打印每级将删的路径、大小、"永不触碰"清单 | 全部 | 0 |
| `outputs` | `results/` `work/` `logs/` `benchmarks/` `.nextflow/history` `.nextflow/cache` `data/cache/` `.nextflow.log*` `report-*.html` `trace-*.txt` `dag-*.html` | 原始数据、参考库、代码、**`.nextflow/conda`(8 个环境)** | 约 38 分钟计算,**无下载** |
| `all` | 以上 + `.nextflow/`(含 conda 环境)+ `.nxhome/` | 原始数据、参考库、代码 | 38 分钟 + 重建 8 个环境(约 5.7 GiB) |

### 与最初草案的差异(逐条记录,不粉饰)

| 草案 | 实际 | 原因 |
|---|---|---|
| L1 / L2 两档 | `check` / `outputs` / `all` 三档 | 用户重新定义了语义:"第一个参数清空所有,第二个清产物但留原序列,第三个空跑确认"。`check` 被提为默认档 |
| L1 含 `.nextflow/{cache,history}`、`tests/ci/out/` | `outputs` 删 `.nextflow/{cache,history}` 和 `data/cache/`;`tests/ci/out/` 不进任何档 | **初版把 `cache` 留在 `all`,被用户当场纠正("不只 output,中间产物都要删")** —— 已上移。`tests/ci/out/` 是 CI 的暂存区,用户清它无收益 |
| 任何一级都不删 `data/` | 申报输入是 `data/raw` `data/db` `data/reference`;`data/cache/` 归 `all` | `data/cache/` 只有 AMR guard 写的 12 KiB 缓存,下一次运行会重建;它不是输入。这条差异是**故意的**,不是漏写 |
| "存在 `nextflow` 进程或 `.nextflow/session-*` 锁 → 拒绝" | 实测 26.10.0 运行期间**没有锁文件**(只有 `.nextflow/cache/<uuid>`、`.nextflow/plr/<uuid>`,结束后仍在),故改用 `pgrep -f nextflow.cli.Launcher`;pgrep 不可用时**只告警并继续** | 无锁可查;但 pgrep 不可用这条边界意味着检查可能空转,所以它必须说出来,不能静默通过 |
| 6 条断言 | 50+ 条:双侧断言(删了什么/留了什么)+ 2 个负对照 + 用法错误 + symlink 拒绝 + 跨目录调用 | 清理工具是唯一能毁掉证据的脚本,断言宁可多 |
| — | `--help` 里写明"只删 `results/` 保留 `work/`,`-resume` 会从缓存重生报告(数秒)" | 实测 F17 语义,这是最高频需求,做成建议路径而不是第四档 |

### 保留的机制

1. **允许清单**:只有表里的路径能被删;输入不在表里,写错也删不到。
2. **逐条复核**(删除前一刻再做一次):相对路径、无 `..`、非软链接、父目录 relpath 在仓库内、不是申报输入。
3. **前后盘点**:`data/raw|db|reference` 的文件数与 KiB、`git` 被跟踪文件数量与修改数,清理前后各记一次并 `diff`;不符即 exit 1 并打印差异。
4. **口径**:释放量用 `df` 前后差;`du` 数字标注"上界"。
5. **拒绝**:运行中的 Nextflow、软链接的 `results/`(否则 `work/` 可能是唯一副本)、非终端且无 `--yes`(exit 2,不猜)。

### 负对照(证明守卫真的会拦)

- 无 `--yes` + 非终端 → exit 2,一个字节都没动;
- `results/` 是软链接 → exit 1,软链接目标文件完好;
- 把 `data/raw` 注入**副本**的删除表 → 仍被"申报输入"拒掉(允许清单不是最后一道防线)。

### 遗留

- `pgrep` 分支在本机沙箱内无法实测(`ps`/`pgrep` 被沙箱禁止),CI 与正常终端上可执行。
- 故意不做扩权开关(`--include-db` 之类):4 GB 参考库重建要重新下载,**不给这个按钮**。
- **成本**:约半天(含测试)—— 实际约 1.5 小时(含两条 `-resume` 实测)。

---

## B. 全量重跑一次(验收)— ✅ 已完成(2026-10-22)

### 过程(三次,每一次都留下了东西)

| 次 | 起始状态 | 结果 |
|---|---|---|
| 1 | 清空后(11 个任务真跑) | 主机负载 34–37/8 核,SPADES 单样本 31 分钟(基线 7.4),跑到第 12 个任务用了 55 分钟被叫停 → **F42**(慢的是主机)+ **F44**(槽位按 cpus 记账) |
| 2 | `-resume`(11 缓存) | **BAKTA 立刻报错** `WGS_AMR_GUARD_CACHE: unbound variable` → **F43**:F33 引入的 `export \$VAR=` 从 10-21 起就是坏的,所有 stub 检查都抓不到 |
| 3 | 修 F43 后 `-resume` | **exit 0**,26 个任务,31.5 分钟(仅 9 个真跑 + 其余缓存),负载 3.9–14 |

### 验收结果(逐条对拍基线)

| 指标 | 基线 | 本次 | 结论 |
|---|---|---|---|
| 组装长度 / contigs / N50 | 4.84 Mb / 370 / 159.3 kbp;5.40 Mb / 390 / 244.8 kbp | 4.84 / 370 / 159.3;5.40 / 390 / 244.8 | ✅ 一致 |
| GC | ~50.3% | 50.33 / 50.34 | ✅ |
| 参照比对 genome fraction / identity | 99.38 / 97.37;99.926 / 99.721 | 99.38 / 97.37;99.926 / 99.721 | ✅ 一致 |
| BUSCO | 440/440(438 S + 2 D) | C:100.0%[S:99.5%,D:0.5%],n:440 | ✅ |
| MEMOTE 总分 | 68.1 / 67.8 | 68.1 / 67.8 | ✅ |
| 生长 / 产率 | 0.953 h⁻¹ / 0.529 g·g⁻¹ | 0.953 / 0.529 | ✅ |
| 完整培养基 / 无碳源 / 厌氧 | 66.000 / 0.000 / 0.228 | 66.001、66.000 / 0.000 / 0.228 | ✅ |
| Bakta 注释行数 | 4891 / 5496 | 4891 / 5496 | ✅ |
| 报告体积 | 15.2 MB | 15.4 MB | ✅ |
| F40 排序边 | MULTIQC 必须等 5b | 报告含「Assembly vs reference genome」章节,trace 里 ASSEMBLY_REF 先于 MULTIQC | ✅ |
| AMR guard | O157:H7 触发并兜底 | 触发,12 轮分块,34 CDS 命中、1 条丢弃 | ✅ |

**任务数更正**:两个样本都给了参考基因组,所以是 **26** 个任务(2×11 + 2×5b + growth + multiqc),不是一直写的 25。旧数字对应"只有一个样本带参考"的 `samples.tsv` 口径。

### 遗留

- 本次 31.5 分钟是"9 个真跑 + 17 个缓存",**不能**与 38 分钟的全程基线直接比;可比的全程空载计时还没做(F42 的教训:计时必须附 `loadavg`)。
- F44 的假设(`-profile lowmem` 可能因重叠而更快)未测。

## C. 本地一键 UI — ✅ 功能完成 / ⏳ 计时验收待复测(2026-10-23)

**交付物**:`ui/server.py` `ui/runner.py` `ui/sheet.py`(python3 标准库,零第三方依赖)、
`ui/index.html` `ui/app.js` `ui/style.css`(原生 JS,无构建、无外链)、`bin/pipeline-ui`(启动器)、
`tests/ci/run-ui-check.sh`(237 条断言,35–47 s,CI job `local-ui`)、`docs/ui.md`(契约 + UI-D1..D12 +
实测记录)、README「Local web UI」小节、CHANGELOG 0.2.0、设计文档 D26 / F45–F48。

### 实际形态(与草案逐条对照)

| 草案 | 实际 | 原因 |
|---|---|---|
| 选文件(给路径,不上传) | 服务端目录浏览器(`GET /api/fs`,只列名字+大小) + 手输绝对路径;FASTQ **不复制、不上传** | 浏览器无法读取本地路径;400 MB 上传是这一步最容易做失败的地方,本地服务不需要 |
| 写 `samples.tsv` | 写 `.nextflow/ui/runs/<id>/samples.tsv`,用 `--samples_tsv` 传入;**仓库根 tracked `samples.tsv` 一个字节都不改** | tracked 文件不该被 UI 改写;绝对路径避免复制原始数据 |
| 启动一次 `nextflow run .` | 单任务(409)+ 单实例(flock);`resume` / `fresh` 两种模式;"清空产物"只经 `bin/pipeline-clean outputs --yes` 且需二次确认 | 与"单一输出根"一致;删除权仍只属于允许清单工具 |
| SSE 实时日志 | 日志落盘 + tailer 推送,支持 `from=` 重放、刷新/重启 attach、断线重连 | 管道直连浏览器会在刷新/断线时丢日志 |
| 跑完嵌 iframe | 报告路径由引擎 `onComplete` 行定位(兜底 config/glob);懒加载 15 MB;报告缺失判 `failed(report_missing)` | 不能假设 `results/report/...` 固定(CI 覆盖层会重定向);不能给绿勾 |
| 一键下载整个 `results/` | 流式 tar.gz + 单报告 + 日志 | 435 MB 不能进内存 |
| (草案未列) | 预检(8 项)、失败面板(步骤/样本/exit code/work dir/stderr 尾)、取消、attach/stale、真实失败注入 | roadmap 的验收要求"失败时指出哪一步、为什么";预检只提示、不代替管线裁决(F20) |
| 明确不做 | 未做账号/多用户/配额/HPC/云端/多引擎;另未做运行历史、断点续传、上传、公网监听 | 与"本地单用户、不是公网服务"的定位一致 |

### 关键约束的落实

- **一次只跑一个任务**:UI 内 409 + `flock` 单实例 + 机器级外部 nextflow 检测;`pipeline-clean` 的
  "运行中拒绝清理"因此不会被绕过。
- **不经浏览器上传**:`/api/fs` 只返回目录/文件元数据;没有上传端点。
- **复用现成产物**:iframe 直接加载 MultiQC 的 15 MB 自包含报告,不新建展示层;报告内嵌的
  MEMOTE/QUAST `srcdoc` 照常工作。
- **安全**:只绑 `127.0.0.1`;一次性 token(URL/cookie/header)+ `Origin` 校验;API 一律 `no-store`;
  无通用静态文件服务。

### 验收实测(2026-10-23)

**真实 2 样本运行(通过 UI 从"清空产物"开始)**:

- 起点:2026-10-23 22:36:55 +0800;中止:23:44:49 +0800 → **67.9 分钟时人工中止(完成 10/26 个任务)**;
  既不是 38 分钟基线,也没有达成 45 分钟
- 主机:8 核,loadavg 起跑 1-min 2.22,运行中升至 **24.1**(15-min 均值 11.5);机器只有 **8 GiB RAM**,
  swap 已用 3.8–7.1 GiB(验收计时必须附负载,F42)
- 清空:UI 调用 `bin/pipeline-clean outputs --yes`,删除 13 条路径、回收 1.74 GiB,输入前后盘点一致
- 结果:本次期望 26 个任务,实际**完成 10 个、提交 11 个**,止于 SPADES;管线自身无报错,发布物正常出现
  (4×FASTQC_RAW + 2×TRIMMOMATIC + 4×FASTQC_TRIMMED);报告**未生成**(未到 MULTIQC);
  UI 终态 `cancelled`(人工取消),不是 `done`
- 对拍基线:**未对拍**(运行未完成)。上一轮已验证的基线数字仍记录在本文件 B 节与 CHANGELOG 0.1.0

**失败定位**:真实 stub DAG 上注入 FASTQC 失败 → UI `failed`,`FASTQC_RAW / CI_B_1 / exit 1`,
stderr 尾解析正确;`noreport`(exit 0 无报告)判 `failed(report_missing)`。

**取消 / attach**:取消后进程组消失、`work/` 保留;运行中 kill -9 UI 再启动 → attach 到原 run
(id/pid 不变、日志继续),且仍可取消(真实 Nextflow 与假引擎两条路径均实测)。

**下载**:真实 `results/`(328 MiB)经 UI 导出 324 MiB tar.gz,解包后文件清单逐文件一致、`trace.tsv` 往返逐字节相同;
报告/日志的字节一致在假引擎与 stub 段有断言覆盖。

**45 分钟口径**:**本次未验证、也不宣称达成**。原因不是 UI 或管线,而是主机条件:8 GiB RAM 上
swap 已用 3.8–7.1 GiB,loadavg 9.7–24.1(8 核),SPADES 的单线程 K-mer 图构建阶段被严重饿死
(与 F42 同一签名)。同一轮里已经证明的是:清空(13 条路径/1.74 GiB,输入盘点一致)、10 个真实任务完成、
真实 `results/` 324 MiB 下载逐文件一致且 `trace.tsv` 往返逐字节相同、真实运行取消(<1 s,进程组消失,
`work/` 保留)、kill -9 UI 后 attach(同 run id/pid,完成数从 0 恢复为 10)。
**复测方法**:关闭重负载应用后从 `bin/pipeline-ui` 起、勾选"清空产物",记录起止时间 + `vm.loadavg` +
`sysctl vm.swapusage`;预期 ≤45 分钟(空载基线 38 分钟)。留给下一轮,不在本轮硬凑数字。

### 遗留

- **首次冷启动不计入 45 分钟**:8 个 conda 环境约 5.7 GiB 下载,预检会明确警告;本机环境已就绪
  (8/8)。
- 不做运行历史/多次运行对比、断点续传、队列;失败重试用 `-resume`(UI 提供按钮)。
- UI 依赖两条引擎文本契约(`Submitted process` 行与 `.nextflow.log` 的 `Task completed` 行),
  Nextflow 升级若改格式需要同步改测试;两种来源都有断言(F48)。
- `/api/preflight` 的 `du` 在超大 `results/` 树上可能超过 3 s(未优化)。
- 机器级外部 nextflow 检测会让"两个 UI 同时跑"互相 409 —— 这是契约选择(不做队列)。

### 成本

约 2 天(含测试与一次真实验收)。**实际**:2026-10-23 21:24 计划 → 22:02 冻结契约并并行开工 →
22:36 代码/测试冻结 → 22:36–23:44 真实验收尝试(67.9 min/10 任务处中止,F49) → 当日完成文档与 0.2.0;3 条并行 agent 工作流
(后端/前端/测试)+ Lead 集成。偏差:验收时发现并修复了 F48(真实引擎 stdout 不报完成),
因此验收运行本身的 UI 中途重启过一次(attach),细节见 docs/ui.md。

---

## 为什么按这个顺序

`A → B → C`。清空让重跑变成零成本动作;重跑证明"从零到报告"是可靠的;UI 只是给这条已经可靠的路径套一层壳。反过来做(先做 UI)会在一个还没有验证过的地基上盖房子。
