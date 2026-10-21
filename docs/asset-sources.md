# 复用资产来源记录（决策 D4：手动副本 + 记录来源 commit）

| 项 | 值 |
|---|---|
| 源仓库 | `../WGSpipeline`（git；相对本仓库根目录） |
| 来源 commit | `75f1d2108a9b06f91034c9c7575ec2c97a7a1e78`（`75f1d21`，2026-10-01，"Split the readme into English (readme.md) and Chinese (readmeZh.md)"） |
| 复制时间 | 2026-10-14（里程碑 1） |
| 复制方式 | 字节一致副本（`cmp` 逐文件校验通过，sha256 见下） |
| 同步策略 | 手动同步（开放问题 #1 已定）；上游改动时重新复制并更新本表 |

## 复制清单与校验和

| 目标路径 | sha256 |
|---|---|
| `envs/bakta.yaml` | `2191a3d1d2ff1de661032dc867cd620081ce48bb1a19c250885a218db9259539` |
| `envs/busco.yaml` | `ddce90b4b7c25901a7b007957bb753dd9eca323194847554fa949d53314fb19d` |
| `envs/carveme.yaml` | `256957fec180e0c6e8fb76d9a3d515443bd9e3c90fb8721e7bde45f2401dcb38` |
| `envs/fastqc.yaml` | `4e08a9d1bb3c421fe8f17b0bcb54340338106fb5dc5410c91c994ff0d61920fc` |
| `envs/memote.yaml` | `2fea3afaaab6b623b8a33610142dd4e7310c71bf5731352af5553d3b19cdb68d` |
| `envs/quast.yaml` | `872830a30a2ae9edf254d1d8398ff00979719684aaf318e201d7421d24092a23` |
| `envs/spades.yaml` | `1032821e3e7d147683884ce629f11469ecb0e80ec7dc4452eca44b24461a0dbe` |
| `envs/trimmomatic.yaml` | `7843ca0acbac13c091b25720fd4ed0da90890dc4dcd8466f493527009745e849` |
| `scripts/multiqc_extras.py` | `feaddfc8048bccab3dd277dc46802f2464510c83901324aa3478c8e22835a795` |
| `scripts/memote_report.py` | `1df2624050b7c2ce8b10c51ba421f37a2db60c67e6c5b1e6312ef7543a6b0a68` |
| `scripts/bin/bakta` | `04017dd727cb8e616e6454027696ad027c0d7fe92afef19e8bc56fd20f9b32fc` |
| `scripts/amr_guard/amr_chunk_guard.py` | `2f682299aaafef44a391ab3bd08430795dfc88e1868d9a50e009eb7afa4ae899` |
| `multiqc_config.yaml` | `4b6c671385ef22dabc3d7848a454d868f2ff782499550990c1e00303feb16b9a` |
| `samples.tsv` | `cb9c883250ee44b928c779a5a1be8461c9d8155b7043cce88575bde23772b2e0` |

## 说明

- `scripts/__pycache__` 未复制（运行时产物，非资产）。
- `raw/`、`db/` 不复制，默认以 `nextflow.config` 的 `params.raw_dir` / `params.bakta_db` / `params.busco_db` 指回源仓库（开放问题 #2）。
- 上游改动 → 重新复制对应文件、更新本表 commit 与校验和。

## 外部数据资产（不复制进仓库，按需获取）

上面是**复制进仓库**的代码/配置资产。下面这些是**按需获取、不提交**的数据，凡是管线行为依赖它们的，都在这里记录可核对的指纹（F30/D20 的做法：路径 + 版本/登录号 + 校验和）。

| 资产 | 用途 | 指纹 | 获取方式 |
|---|---|---|---|
| `carveme/data/input/media_db.tsv` | `media/m9_aerobic.yaml` 的 20 个化合物取自该库的 `medium == M9` 行 | carveme **1.6.6**（包内文件）<br>sha256 `fab9ebde307ec626473fa8f546d22227b28f32289ecd4e26ea79d8cf086b78a2` | 随 `envs/carveme.yaml` 建环境时自动就位 |
| `GCA_000005845.2` | 参照 1：*E. coli* K-12 substr. **MG1655**，complete genome | 4,641,652 bp，1 条（`U00096.3`）<br>md5 `b62abb256585725bb26065df188f355b` | NCBI Datasets：`/genome/accession/GCA_000005845.2/download?include_annotation_type=GENOME_FASTA` |
| `GCA_000008865.2` | 参照 2：*E. coli* **O157:H7 str. Sakai**，complete genome | 5,594,605 bp，3 条（染色体 `BA000007.3` + pOSAK1 + pO157）<br>md5 `1fd428ef2643c351c25ade264d5531ec` | 同上，换登录号 |

参照基因组落在 `data/reference/`（`/data/` 已在 `.gitignore` 中），下载命令见 README 的 *Assembly validation* 一节。

**已知的未知**：CarveMe 包里**没有**关于 `media_db.tsv` 出处（它是从哪些文献/模型收集来的）的任何说明或引用。因此"CarveMe 的 M9 行本身从哪来"这一层未被本项目核实；本项目能追溯的是"这 20 个化合物确实来自 carveme 1.6.6 的该文件，且其 sha256 如上"。若要对外声明更上游的可引用性，需要另行查证上游源码仓库。
