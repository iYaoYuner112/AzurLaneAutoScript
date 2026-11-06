# AGENTS.md — ALAS fork 维护入口

新会话先读。这里只放稳定规则和入口；版本、测试数、当前状态现场查。
本项目是 ALAS（AzurLaneAutoScript）的本地 fork；实例常在另一台机器。

## 开工

- 改文件前先 `git status -sb`，确认没有未提交改动；涉及上游合并/差异时再
  `git fetch upstream` 并比较 `HEAD..upstream/master`，日常任务不必每次查。
- upstream = LmeSzinc/AzurLaneAutoScript，唯一官方上游。
- [AzurPilot](https://github.com/wess09/AzurPilot) 是另一套本地镜像，只做行为参照；
  路径用 `$AZURPILOT_PATH`，没有就问用户，不要猜。
  只有 AzurPilot 对齐、差异审计或搬功能时才 fetch，命令见 `doc/workflows.txt`。
- 存在 `.workbuddy/memory/` 时先读其中旧结论，只当线索；
  引用前用当前代码/AzurPilot 复核，旧日期和行号会过期。
- 测试：见下文“测试”小节。
- 具体开发问题：先查 `doc/dev-reference.txt` 的主题索引，只读对应章节/代码，不要整篇加载。

## 铁律

1. 官方上游只有 upstream。不要把 AzurPilot 的分支合并进 master。
2. 测试通过（或说明无适用测试）、用户明确说“提交”或“推送”后才执行对应动作。
   一次授权只对那批改动生效；“做吧/改吧/修吧”不是授权；多主题分开提交。
3. origin URL 带凭据：不要打印 `git remote -v`，不要外发报错堆栈或截图。
4. 不做破坏性 git 操作（force push、reset --hard、删除未提交文件）；覆盖用户未提交改动前先确认。
5. 一个主题/可验证的小功能（或用户一次指定的一批）做完、测试通过（或说明无适用测试）
   后先停下报告；确认继续不等于授权提交。未确认前不开始下一个主题、不夹带无关大改；
   跨文件大改先说明范围。

## 已定决策与踩坑

动手前先读 `doc/decisions-and-pitfalls.txt`；新决策/新坑写那里并写日期，不扩本文件。

## 流程入口

- 合并官方上游、回退与恢复、搬 AzurPilot、AzurPilot 差异审计：见 `doc/workflows.txt`，按任务只读对应段。
- 用户贴日志/报错时：按 `doc/workflows.txt` 第五节；先还原时间线和调用栈，
  再查 AzurPilot 当前版；未批准不改代码。
- 通用底线：合并前做整仓三方演练；搬 AzurPilot 前查回档；回退后用 diff 验证；共享判断实机验证。

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

## 配置、运行与硬约束

- 项目约定的 Python 环境：优先用 `deploy.yaml` 的 `PythonExecutable`（默认 `./toolkit/python.exe`）；该路径不可用或未配置时，以用户当次说明为准，未说明就问用户。
- 运行：用上述 Python 环境运行 `alas.py`（调度器）、`gui.py -p <端口>`（WebUI）。
- 依赖优先用上述 Python 环境，不要装系统 Python；需要重装时再参考 `requirements.txt`。
- 配置源：`module/config/argument/*.yaml`、`module/config/i18n/*.json`；改完在仓库根目录用
  上述 Python 环境运行 `module/config/config_updater.py`，不要只手改 `config_generated.py`。
  源文件职责、新增配置流程和服务器/包名见 `doc/dev-reference.txt` 的“配置系统”。
- `config/*.json`、`log/` 是运行期数据，不提交；资产按 `assets/<server>/`，
  按钮图用 `dev_tools/button_extract.py`。
- 不做任务外扩展；没有数据源就不显示，禁止伪造数据。
- 改动最小、可回退；行为修复按 AzurPilot/现有默认，新开关默认关闭。
- UI/图标实际渲染验证；新增日志优先 debug，不影响判定。

## 测试

- 用上述 Python 环境运行 `tests/run_os_tests.py [单文件/目录]`，并设置
  `PYTHONIOENCODING=utf-8`（bash 前缀，cmd 用 `set`，PowerShell 用 `$env:`；Windows 应用商店的 `python` 别名可能不可用）。
  `tests/island`、`tests/shop_event`、`tests/dev_tools` 等 pytest fixture/截图测试不在 runner 范围。
  测试选择自行判断：`tests/os` 必跑，pytest 有环境就跑目标用例，没有就记录“未运行 + 原因”，
  不为此询问用户，也不往其他项目/系统环境装包；找不到可用解释器时先做静态检查并写明测试未运行。
  失败先报告具体项，不要继续提交；测试数按当次输出写，不要沿用旧数字。
  改动补离线测试，修 bug 配最小回归测试，断言约定见 `doc/decisions-and-pitfalls.txt` 的“测试约定”。
- 确有必要的 pytest/截图测试：优先用已有环境；缺 pytest 时可在临时隔离环境安装最小依赖，
  跑完即弃，不改现有项目/系统环境；安装或网络失败则记录“未运行 + 原因”，不阻塞。
- 改配置相关测试用临时配置目录，避免写用户 `config/alas.json`。

## 提交

- 提交不带临时文件、令牌、本机路径/用户名；临时截图、日志、录屏、音频、视频不进 Git；`assets/<server>/` 等游戏识别素材和已登记图标按现有资产流程提交。
- 测试用回归截图统一放 `tests/os/fixtures/<主题>/`，注明 1280×720 来源和当时期望行为，不属于“临时截图”。
- 标题单行中文，只描述改动，不用“测试/日志”等过程词凑数；确实是测试/日志本身的改动可以写。

## 模块地图

- 底层：`module/base/`、`module/map_detection/`、`module/ocr/`
- 大世界：`module/os/map.py`、`fleet.py`、`map_event_resume.py`、`tasks/scheduling|daily|explore|meowfficer_farming.py`
- 事件/战斗/商店：`module/os_handler/`、`module/os_combat/`、`module/os_shop/`
- 续战：`module/campaign/run.py`、`campaign_base.py`、`recovery.py`、`alas.py`
- 配置/UI/设备：`module/config/`、`module/ui/`、`module/webui/`、`webapp/`、`module/device/`
- 测试：`tests/os/`，入口 `tests/run_os_tests.py`

## 维护

- 只有规则变化才更新本文件，不要每次提交都改。
- 汇报用中文、结论先行；审计逐项讲作用/适用性，再问做不做。
- 交付时列出：改动文件清单、测试结果、需要用户实机验证的点（静态测试覆盖不到的）。
- 本文件不替代用户当场任务说明。
