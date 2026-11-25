# 分子优化构型：真实制图资料

六组均找到实际优化收敛并正常结束的 Gaussian 16 Rev. A.03 输出。复合物方法为 B3LYP-D3(BJ)/6-311G(d,p)，Counterpoise=2，SCF=Tight，NoSymm；逐组实际优化设置以原日志为准。没有复合物频率计算证据，虚频及是否为严格局部极小值均未核实。

论文数据基准：ΔE = E_CP(复合物) − E_opt(单体1) − E_opt(单体2)，采用 2625.499638 kJ/mol/Eh。六组均复现原稿的两位小数。Gaussian 直接打印的 corrected complexation energy 以复合物几何下的片段能量和为基准，两列不能互换。E_CP 是总能量，不能直接当成结合能。证据来自原稿段落、独立单体日志、复合物日志、项目导入表和几何对照。GBL—甲醇有另一条正常结束的历史记录，舍入后相同；这里按正式导入记录的精确 E_CP 与配套 NCI FCH 选取。

| 体系 | job ID | 原稿 kJ/mol | 复算 kJ/mol | 最近片段间原子对 | 距离 Å |
|---|---|---:|---:|---|---:|
| EC—乙醇 | ethanol_ec_cp | -26.10 | -26.102495 | O1···H19 | 2.5991 |
| EC—水 | h2o_ec_cp | -24.11 | -24.108924 | H3···O5 | 2.2711 |
| PC—C₃F₈ | c3f8_pc_cp | -3.54 | -3.537474 | F5···H23 | 2.9356 |
| PC—C₃F₆ | c3f6_pc_cp | -8.04 | -8.044152 | F4···H21 | 2.6374 |
| GBL—甲醇 | methanol_gbl_cp | -30.99 | -30.986929 | H6···O8 | 1.9426 |
| GBL—THF | tetrahydrofuran_gbl_cp | -29.26 | -29.264856 | H13···H22 | 2.4784 |

各组 `核查说明.md` 给出完整来源、精确行号、构型取舍和缺失项目。原始 Gaussian、Multiwfn、VMD 文件均为副本，哈希可查。没有另找到需要采用的原始最终 XYZ；包中 final.xyz 是从核实的 FCH 提取的派生文件，CSV 保留 Gaussian 1 起始编号与 CP 片段，VMD 内部 index 从 0 开始。

用于总体流程图：优先用各组 `structure/ball_stick_2400x1800.png`。它们是 VMD 1.9.3 的 Tachyon 白底输出，六组采用统一正交尺度和元素配色（C 深灰、H 浅灰、O 红、F 绿）；仅用显示变换选择视角，最终坐标未旋转、平移或变形。VMD 在 Tcl Console 用 `source {完整路径/structure/view_ball_stick.vmd}` 加载可编辑视图；脚本按其自身位置载入 final.xyz，分子内连线用坐标及共价半径核查，未连跨片段化学键。最近接触及全部片段间距离见 CSV，不将距离单独作为氢键判据。

各组 `nci_original/nci.png` 是已有 NCI/RDG 等值面原图，保留原尺寸与原分析脚本、两份 cube、FCH 和日志。通过 `view_nci.vmd` 可编辑历史等值面视图；该派生脚本仅调整相对路径并移除自动渲染与退出。项目原始 NCI 标记为 low 网格档，未重算，也未发现独立 RDG 散点图。本次未运行 Gaussian、Multiwfn 或 MD。

对于含 O–H 的复合物，另附跨片段 O–H···O 的几何量表（含较远、偏离线性的组合，便于排除不合适的归类）。历史 Multiwfn 日志标明 func1.cub 是 sign(λ₂)ρ，func2.cub 是 RDG；历史视图以 RDG = 0.5 绘制等值面并用 sign(λ₂)ρ 着色。

PC 两组的 LOG、CHK、FCH 另与 `F:\萃取剂筛选_迁移包_20260902\01_核心筛选项目\aspen_ed_screening` 内同任务相对路径的六份文件比对，SHA256 全部一致，迁移副本不重复打包。
