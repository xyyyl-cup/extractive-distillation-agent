# 六组分子动力学MSD/RDF原始结果交付

整理日期：2026-11-03（Asia/Taipei）。本次未修改原始计算文件、论文或现存索引；未运行模拟、MSD或RDF重提取。
仅运行GROMACS 2018.8的帮助查询、gmx dump和gmx check，检查的对象是新文件夹中的副本。

## 首先查看

1. 体系核查总表.csv：六组任务身份、参数和结果状态。
2. curve_index.csv：每条曲线的文件位置、真实分析对象、单位和统计区间。
3. 各体系/说明.md：来源完整路径、分组编号/原子名称、计算记录、缺失问题。
4. 原扩散系数与拟合区间.csv：原XVG中的D及误差和拟合时间，保留历史计算值。

## 核实结论

- 六组的system.top和md.tpr均为300个萃取剂+10个待分离分子，设定298.15 K、1 bar、2 ns，与原稿这些条件一致。温压是设定值，未将其称为轨迹平均值。
- 六组生产轨迹md.xtc均可读，0–2000 ps共401帧，5 ps间隔；md.log均有正常结束记录。对应md.tpr、md.gro、md.mdp、index.ndx、md.edr以及拓扑均已复制。
- 找到6条MSD和4条RDF历史曲线；CSV完整导出，共10条本轮曲线。其中EC—水的MSD实际属于EC，水的MSD仍缺失。
- PC—C3F8与PC—C3F6未找到RDF/配位数；轨迹、TPR和索引可支持重新提取，但需要确定全原子、逐分子质心或具体原子对的定义。本次没有生成新曲线。
- EC—水有确证的分组错误：原分析日志Selected 4: MySolute，MySolute=3000个EC原子；MySolvent=30个水原子。msd_H2O.xvg不能标成水MSD。RDF实际为EC全原子→水全原子。CSV文件名及列名均突出ACTUAL以避免误用，原XVG保持原样。详见EC水_分组错误证据.txt。
- 所有现有MSD使用MySolute全原子组、GROMACS默认质量加权；没有-mol，不是分子质心MSD。统计轨迹1000–2000 ps，横轴lag time为0–1000 ps、单位ps，纵轴nm²。扩散拟合lag time为100–900 ps。
- 已有RDF为全原子对RDF，包含氢原子；没有mol_com、原子名称筛选或配位数-cn输出。读取0–2000 ps全轨迹，距离nm、g(r)无量纲。统计区间由原命令、轨迹覆盖范围和2018.8默认值共同核实。
- GBL—甲醇中的JC经case_config、GRO和ITP原子组成CH4O交叉核实为甲醇；EC是C3H4O3、PC是C4H6O3、GBL是C4H6O2、THF是C4H8O，未仅凭目录名判断。
- GBL—THF原XVG记录的运行位置为D:\ed_gmx_runs\tetrahydrofuran_gbl_legacy_25840_20260708_185708；项目中保存了该次运行的副本。本次另核对两个位置的重要文件哈希，见THF运行副本比对.csv。

## Origin使用

进入各体系origin_csv/，导入UTF-8 CSV，第一行为列名；第一列X、第二列Y。数字字符串逐行原样保留，未做平滑、删点、插值或单位换算。
曲线长度不同属原始输出特征，RDF末端距离没有强行裁齐。原XVG全部注释保留于original/。
04_EC_H2O目录中的ACTUAL_EC_NOT_WATER曲线只可作为EC的原子MSD，不能补入“水的MSD”图。
historical_not_selected/仅是另一轮历史归档，不与本轮数据合并；其MSD/RDF也单独导出CSV，未核实与历史轨迹对应关系。

## 追溯与完整性

source_manifest.json记录每个原始副本的源完整路径、包内路径、大小、SHA-256。
核查证据/保存2018.8本机帮助；各体系verification/保存只读的TPR转储与轨迹检查。
完整性验证.json记录原始副本哈希、CSV逐行比对、ZIP验证结果。没有用截图或示例数据替代计算结果。
未检查论文每个最终图号与这些任务的对应关系；也没有把文件名当作分析对象的唯一依据。
