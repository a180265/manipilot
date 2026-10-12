# gesture-program.v1 — 单指手势程序

版本：`gesture-program.v1`（规范第 6 节）。

- 顶层仅 `program_version / coordinate_space / strokes` 三字段，必填。
- `strokes` 1-4 段；每段 `points` 1-64 点，点有且仅有 `x/y/t_ms`（归一化 [0,1]）。
- 每段首点 `t_ms=0`、之后严格递增（由规划器在 schema 外强制）；
  同坐标不同 t_ms 表示接触停留。
- `hold_before_ms/hold_after_ms ∈ [0,2000]`；`gap_after_ms ∈ [0,1000]`；末段 gap 必须为 0。
- 每段接触计划 ≤5000ms；整程序接触与间隔之和 ≤8000ms（规划器强制）。
- 不接受 Z、tapDepth、tapFeed、G-code、URL 回调、脚本、pressure、device 切换。
- 双击继续走 TAP2 快速宏，不用两段程序拼。

执行：P1/P2 两点直线编译为现有 `SWIPE`；多段路径需 daemon `path.v1` 特性与
`PATH1` 宏（P3），未加载时返回 `path_feature_unavailable`，禁止拆成多次 SWIPE。
