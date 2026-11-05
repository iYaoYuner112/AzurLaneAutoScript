# AGENTS.md — ALAS fork 维护入口

新会话先读。这里只放稳定规则和入口；当前状态、版本、测试数一律现场查。
最后更新：2026-11-05。
主要场景：B 站客户端（com.bilibili.azurlane）、GemsFarming 长期挂机、大世界调度；实例常在另一台机器。

## 开工

```bash
git status -sb
git log --oneline -8
git fetch upstream
git log --oneline --date=short --format="%h %ad %s" HEAD..upstream/master
```

- upstream = LmeSzinc/AzurLaneAutoScript，唯一官方上游。
- AP 是另一套本地镜像，只做行为参照；路径优先用 `AZURPILOT_PATH`，没有就问用户，不要猜。
  用户要求 AP 对齐、差异审计或搬功能时，才 fetch 并记录版本；日常任务不用每次查。
  ```bash
  git -C "$AZURPILOT_PATH" fetch --all
  git -C "$AZURPILOT_PATH" log -1 --format="%h %ad %s" master
  git -C "$AZURPILOT_PATH" log -1 --format="%h %ad %s" origin/dev
  ```
- 先查项目记忆和 `.workbuddy/memory/` 旧结论，别重复调研。
- 测试：`PYTHONIOENCODING=utf-8 python tests/run_os_tests.py [单文件/目录]`。
  `tests/island` 等截图/UI 测试需要 pytest 和素材，不在此 runner 范围。
  失败先报告具体项，不要继续提交；测试数按当次输出写，不要沿用旧数字。
  找不到 Python 解释器或依赖时问用户，不要猜或往系统环境乱装。
- 具体开发问题：先查 `doc/dev-reference.txt` 的主题索引，只读对应章节/代码，不要整篇加载。

## 铁律

1. 官方上游只有 upstream。不要把 AP 的分支合并进 master。
2. 测试通过、用户明确说“提交推送”后才提交/推送。一次授权只对那批改动生效；
   “做吧/改吧/修吧”不是授权；多主题分开提交。
3. origin URL 带凭据：不要打印 `git remote -v`，不要外发报错堆栈或截图。
4. 不做破坏性 git 操作（force push、reset --hard、删除未提交文件）除非用户明确要求。

## 已定决策与踩坑

动手前先读 `doc/decisions-and-pitfalls.txt`：已定决策、刻意偏差和踩过的坑都在那里。
本文件只保留稳定规则和入口；新决策或新坑追加到该文件，不要扩这里。

## 流程入口

- 合并官方上游、回退与恢复、搬 AP、AP 差异审计：见 `doc/workflows.txt`，按任务只读对应段。
- 用户贴日志/报错时：按 `doc/workflows.txt` 第五节；结论先行，先还原时间线和调用栈，
  再查 AP 当前版；未批准不改代码。
- 通用底线：合并前做整仓三方演练；搬 AP 前查回档；回退后用 diff 验证；共享判断实机验证。

## 游戏交互代码约定

- 游戏识别基于 1280×720 截图，资源和坐标都按这个分辨率。
- 状态循环：截图→识别→操作；退出用 `appear()`，操作用 `appear_then_click()`，
  点击 interval 一般 2~5 秒。
- 退出条件只用 `appear()` 正面识别，不带 interval；不要用 `appear_then_click`、
  `handle_*` 或负面条件退出。
- 识别调试可脱离游戏：给 `device.image` 加载本地截图；模块尽量可独立调用，
  只有任务入口依赖用户配置。
- 不在状态循环里 sleep 猜界面、不嵌套状态循环；`handle_*` 返回 bool，
  True 表示已操作、需要新截图。
- 复用 `GameStuckError`、`GameTooManyClickError` 和上层恢复，不吞异常。
- 页面跳转用现有 UI/Page；docstring 用 `Pages:` 标注进入/退出页面。
- 改配置相关测试用临时配置目录，避免写用户 `config/alas.json`。

## 配置、运行与硬约束

- 运行：`python alas.py`（调度器）、`python gui.py -p <端口>`（WebUI）。
- 依赖：`pip install -r requirements.txt`；优先用项目约定环境，不要装系统 Python。
  Windows bash 用 `PYTHONIOENCODING=utf-8 python ...`；cmd 用 `set PYTHONIOENCODING=utf-8`，
  PowerShell 用 `$env:PYTHONIOENCODING="utf-8"`。
- 配置源：`module/config/argument/*.yaml`、`module/config/i18n/*.json`；改完在仓库根目录运行
  `python module/config/config_updater.py`，不要只手改 `config_generated.py`。
  源文件职责、新增配置流程和服务器/包名见 `doc/dev-reference.txt` 的“配置系统”。
- `config/*.json`、`log/` 是运行期数据，不提交；资产按 `assets/<server>/`，按钮图用 `dev_tools/button_extract.py`。
- 不做任务外扩展；没有数据源就不显示，禁止伪造数据。
- 改动最小、默认关闭或可回退，补离线测试，按主题分开提交。
- UI/图标实际渲染验证；新增日志优先 debug，不影响判定。
- 提交不带临时文件/日志/截图/令牌/本机路径/用户名。
- 提交标题单行中文，只描述代码改动；不要写“测试/日志”字样。
- 实机顺序：本机 AlasApp pull→重启→实测→云手机 pull；改完必须重启进程。

## 模块地图

- 底层：`module/base/`、`module/map_detection/`、`module/ocr/`
- 大世界：`module/os/map.py`、`fleet.py`、`map_event_resume.py`、`tasks/scheduling|daily|explore|meowfficer_farming.py`
- 事件/战斗/商店：`module/os_handler/`、`module/os_combat/`、`module/os_shop/`
- 续战：`module/campaign/run.py`、`campaign_base.py`、`recovery.py`、`alas.py`
- 配置/UI/设备：`module/config/`、`module/ui/`、`module/webui/`、`webapp/`、`module/device/`
- 测试：`tests/os/`，入口 `tests/run_os_tests.py`

## 维护

- 不要每次提交都改本文件。
- 只有规则变化才更新本文件；决策和新坑写 `doc/decisions-and-pitfalls.txt`，并写日期。
- 流程细节写 `doc/workflows.txt`，开发参考资料写 `doc/dev-reference.txt`。
- 当前状态用命令查：git log、HEAD..upstream/master、AP master/dev、测试输出。
- 汇报用中文、结论先行；审计逐项讲作用/适用性，再问做不做。
- 交付时列出：改动文件清单、测试结果、需要用户实机验证的点（静态测试覆盖不到的）。
- 本文件不替代用户当场任务说明。
