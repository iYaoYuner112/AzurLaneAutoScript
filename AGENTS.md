# AGENTS.md — ALAS fork 维护入口

新会话先读。这里只放长期有效的规则和入口；当前状态、版本、测试数一律现场查。
最后更新：2026-11-04。
主要场景：B 站客户端（com.bilibili.azurlane）、GemsFarming 长期挂机、大世界调度；实例常在另一台机器。

## 开工

```bash
git status -sb
git log --oneline -8
git fetch upstream
git log --oneline --date=short --format="%h %ad %s" HEAD..upstream/master
```

- upstream = LmeSzinc/AzurLaneAutoScript，唯一官方上游。
- AP 是另一套本地镜像，只做行为参照；路径优先用环境变量 `AZURPILOT_PATH`，
  没有就问用户，不要猜。先确认版本再对照：
  ```bash
  git -C "$AZURPILOT_PATH" fetch --all
  git -C "$AZURPILOT_PATH" log -1 --format="%h %ad %s" master
  git -C "$AZURPILOT_PATH" log -1 --format="%h %ad %s" origin/dev
  ```
- 先查项目记忆和 `.workbuddy/memory/` 旧结论，别重复调研。
- 测试：`PYTHONIOENCODING=utf-8 python tests/run_os_tests.py [单文件/目录]`。
  `tests/island` 等截图/UI 测试需要 pytest 和素材，不在此 runner 范围。
  失败先报告具体项，不要继续提交；测试数按当次输出写，不要沿用旧数字。
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

## 合并官方上游

1. 工作区干净，在 master；逐提交看 diff，重点 os/combat/campaign/alas.py/配置生成物。
2. 做整仓三方演练，不只看文本冲突：
   ```bash
   tree=$(git merge-tree --write-tree HEAD upstream/master | head -1)
   git diff --stat HEAD "$tree"
   git diff HEAD "$tree"
   ```
   逐条确认没有覆盖历史定制。
3. 重灾区：`module/os/map.py`、`globe_operation.py`、`tasks/scheduling|explore|daily`、`alas.py`、`campaign/run|campaign_base`、`config/argument`。
4. 共享判断（is_in_map、经验页、关卡页确认）会横向影响老定制，必须实机验证港口/要塞雾天和 GemsFarming。
5. 合并后跑全量测试；用户明确说“推送”后才推送。

## 回退与恢复

- 批量回退用 `git revert --no-commit <提交>...`，完成后必须
  `git diff <回退目标提交> --stat` 为空才算完成。
- 恢复此前回退的内容后，必须 `git diff <当时的提交> HEAD --stat` 逐文件核对忠实度。
- 回退/恢复后跑全量 tests/os，基线数字按当次输出更新。

## 搬 AP

1. 以 AP 当前 master/dev 代码和调用链为准，不能只看旧提交 diff；旧结论会过期。
   AP 合官方上游是“几天一批”，未合同批时我们领先不算偏差；AP 配置默认值不等于用户实跑值，必要时看 AP 的 `config/alas.json`。
2. 搬前查回档/重写：
   ```bash
   git -C "$AZURPILOT_PATH" log --all --oneline --grep="Revert"
   git -C "$AZURPILOT_PATH" log --all --oneline -S"<关键函数名或常量>"
   ```
   已被删掉的行为不要搬（相机、明石踩过）。
3. 涉及配置绑定、对象生命周期、跨任务状态的方案，**动笔前先实测三件事**（2026-11-04 轻量版
   方案因此被否）：
   - `bind()` 后该任务的 config 能读到哪些键（按任务分组绑定，Restart 读不到 Campaign 组）；
   - 写入的键会不会被 `config_updater` 剪掉（它从空字典只遍历注册键，未登记即删）；
   - 绕过某段初始化后，对象上还剩哪些属性（`self.map`、`map_is_auto_search` 之类都会缺）。
4. 说“AP 没有 X”必须给当前分支函数名/行号；实现不同但效果相同时要写清差异。
5. 只对齐效果，不要求目录/命名/配置位置一致。
6. 适配本仓库：
   - 调度代理会换 `config.task`，注意任务上下文。
   - 续战保留原 `CampaignRun` / `CampaignBase` 对象，不能重建后跳过初始化。
   - TaskEnd 不是 ScriptEnd；容错放行 `UNSWALLOWABLE_ERRORS`（`module/os/map.py:57`）。
   - `cross_get` 的 default 只在缺键时生效，须等于生成默认；关闭显式存 false。
   - 配置生成器会删未登记 i18n；自用文案先登记。
7. AP 删掉/回档的加固不要以“更稳”恢复；用户说“AP 是这样就不用改”就保持。

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
- 配置源：`module/config/argument/*.yaml`、`module/config/i18n/*.json`；改完在仓库根目录运行
  `python module/config/config_updater.py`，不要只手改 `config_generated.py`。
  职责：`task.yaml` 任务/菜单，`argument.yaml` 参数/默认/校验，`default.yaml` 任务默认值，
  `override.yaml` 强制覆盖，`gui.yaml` GUI 翻译键。配置路径是 `<Task>.<Group>.<Argument>`，
  绑定后通过 `self.config.Group_Argument` 访问。
- `config/*.json`、`log/` 是运行期数据，不提交；资产按 `assets/<server>/`，按钮图用 `dev_tools/button_extract.py`。
- 不做任务外扩展；没有数据源就不显示，禁止伪造数据。
- 改动最小、默认关闭或可回退，补离线测试，按主题分开提交。
- UI/图标实际渲染验证；新增日志优先 debug，不影响判定。
- 提交不带临时文件/日志/截图/令牌/本机路径/用户名。
- 提交标题单行中文，只描述代码改动；不要写“测试/日志”字样。
- 实机顺序：本机 AlasApp pull→重启→实测→云手机 pull；改完必须重启进程。

## 模块地图

- 大世界：`module/os/map.py`、`fleet.py`、`map_event_resume.py`、`tasks/scheduling|daily|explore|meowfficer_farming.py`
- 事件/战斗/商店：`module/os_handler/`、`module/os_combat/`、`module/os_shop/`
- 续战：`module/campaign/run.py`、`campaign_base.py`、`recovery.py`、`alas.py`
- 配置/UI/设备：`module/config/`、`module/webui/`、`webapp/`、`module/device/`
- 测试：`tests/os/`，入口 `tests/run_os_tests.py`

## 维护

- 不要每次提交都改本文件。
- 只有规则变化才更新本文件；决策和新坑写 `doc/decisions-and-pitfalls.txt`，并写日期。
- 当前状态用命令查：git log、HEAD..upstream/master、AP master/dev、测试输出。
- 汇报用中文、结论先行；审计逐项讲作用/适用性，再问做不做。
- 交付时列出：改动文件清单、测试结果、需要用户实机验证的点（静态测试覆盖不到的）。
- 本文件不替代用户当场任务说明。
