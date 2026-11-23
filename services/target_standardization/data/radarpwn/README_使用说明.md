# RadarPWN TTM标准化开发数据包（已核验）

来源：https://zenodo.org/records/7188636 ，RadarPWN v1.0.2。
原压缩包：radarpwn-1-0-1.zip。
原始成员：RadarRostock-Benign-03/captures/RadarRostock-Benign-03-20220603163049.pcap.gz

这是BridgeCommand / OpenCPN仿真环境记录的网络通信，不是物理雷达实测数据。
选择Benign场景。未生成或修改任何传感器报文；报文保持从UDP目的端口10110提取的原始ASCII及出现顺序。附加CSV仅增加PCAP记录时间和校验结果。

## 已确认内容

- radar_ttm.nmea：383条TTM；校验和383/383通过。
- 连续来源内目标编号00，共1个目标；所有TTM状态均为T。
- ais.nmea：7719条AIS NMEA语句；语句条数不等于解码消息或独立船舶数（消息可能分片）。
- all_incoming_nmea.nmea：同时提取位置、艏向、日期时间等其他原始NMEA，便于后续核验参考与时间。
- *_with_capture_time.csv：每条报文对应的PCAP记录时间（Unix秒、UTC字符串）、报文类型、原始语句及校验结果。
- inspection_summary.json：统计、首末TTM及来源信息。
- original/：原始抓包，便于独立复核。

## 时间处理必须保留两条时间线

PCAP记录时间在2022-06-03；NMEA ZDA声明的仿真日期为2019-09-30，TTM携带的是仿真时刻的UTC日内时间。不得将2022年的抓包日期直接拼接到TTM仿真日内时间，也不得把抓包时间自动认作目标状态时间。ZDA与TTM配对、仿真速度、暂停及接收延迟尚未进行完整评估。

首次TTM抓包时间：2022-06-03T16:31:48.952602+00:00
末次TTM抓包时间：2022-06-03T16:49:06.967893+00:00

## 使用范围

可用于TTM字段解析、校验和、单位换算、参考标记、时间来源保留、标准记录输出与接口衔接。不足以证明真实设备兼容性、多目标跟踪性能、跨源关联精度或现场时空对齐精度。未证明目标00与某个AIS MMSI的对应关系。

TTM示例：
$RATTM,00,0.1,9.7,T,4.0,2.7,T,0.0,0.3,N,TGT00,T,,150054.00,A*48

## 原始项目引用

Wolsing, Konrad; Saillard, Antoine; Bauer, Jan; Wagner, Eric; van Sloun, Christian; Fink, Ina Berenice; Schmidt, Mari; Wehrle, Klaus; Henze, Martin. Network Attacks Against Marine Radar Systems: A Taxonomy, Simulation Environment, and Dataset. IEEE LCN, 2022. DOI: 10.1109/LCN53696.2022.9843801.

本包为原始公开数据的提取整理，使用与引用要求以原数据发布记录为准。
