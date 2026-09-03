# uptime — 桌面小部件合集（Windows）

一组常驻 Windows 桌面的**无边框置顶小挂件**，做的是"时间/收入可视化"：

| 挂件 | 代号 | 做什么 |
|------|------|--------|
| 烧钱率(burn-rate) | `burn` | 今日/本月已赚、**发薪周期进度条**、距发薪秒级倒计时、金币/美钞小动画 |
| 倒计时(ETA) | `eta` | 距下班大倒计时、今日进度、距下一个节假日的秒级倒计时 |

另有托盘壳（`console`）、可视化面板（`panel`）、假日志终端（`tail`）、全局热键隐藏（`focus`）。本文以两个挂件为主。

---

## 1. 能有什么效果

### burn 挂件（美元纸币风卡片）

- **今日已赚**大字 + **本月已赚**：按月薪、上下班时间、午休、工作日实时算；默认**打码**，鼠标悬停显示真值
- **进度条 = 发薪周期**：
  - 每月 **10 号 18:00** 发薪（10 号若遇周末/法定假日，**前移**到最近的前一个工作日，仍在 18:00 发；调休补班日视为工作日不挪）
  - 发薪那一瞬间进度**归 0**，整月线性爬向满格（下次发薪 = 100%）
- 底部 **距发薪 `X天 HH:MM:SS`**：精确到秒、每一秒贴整秒跳动（终点同上，即"下班那刻算发薪"）
- 动画（仅上班时段，即 `work_start`~`work_end` 之间）：
  - **金币**：每秒在金额右侧抛一枚泰拉瑞亚风像素金币（按官方 Gold Coin 贴图复刻，12×16）
  - **美钞**：每 5 秒从金额右上方抛一小把美钞小方块
  - 都是小动画、无数字、弹起即淡出

### eta 挂件（赛博 HUD 风卡片）

- **大倒计时**：今天距下班精确到秒（`HH:MM:SS`），整秒对齐跳动
- **今日进度条**：上班时间过了百分之多少
- 底部 **`HOL:<名> Xd HH:MM:SS`**：距下一个节假日的倒计时，**终点 = 放假前最后一个工作日的下班时间（18:00）**——即"下班那刻就放假"；不足 1 天只显示 `HH:MM:SS`；正处假期显示 `HOL: now <名>`

---

## 2. 环境要求

- Windows（Win32）
- Python **3.10**（本机用 Windows launcher：`py -3.10`）
- 安装依赖：

```
py -3.10 -m pip install -r requirements.txt
```

---

## 3. 配置（决定效果）

配置文件是普通 JSON。**含月薪等隐私，不入库**（`.gitignore` 已排除）。

| 读取位置 | 说明 |
|---|---|
| 源码运行 | 仓库根 `config.json`（不存在则回退入库模板 `config.example.json`） |
| 打包 exe | `%APPDATA%\uptime\config.json`——首次运行自动用内置模板生成；**exe 旁边不留明文配置** |

从模板复制一份并填自己的值：

```
copy config.example.json config.json
```

字段：

| 键 | 含义 | 示例 |
|---|---|---|
| `monthly_salary` | 月薪（burn 的今日/本月金额算法基础） | `5000` |
| `monthly_workdays` | 月工作日数（算日薪用） | `21.75` |
| `work_start` / `work_end` | 上班 / 下班时间（burn 计费区间、eta 倒计时终点） | `"09:00"` / `"18:00"` |
| `lunch_break_minutes` | 午休分钟（12:00 起，与班内重叠部分不计费） | `60` |
| `workdays` | 每周哪几天上班（0=周一 … 6=周日） | `[0,1,2,3,4]` |
| `widgets.always_on_top` | 挂件是否置顶 | `true` |
| `widgets.burn_pos` / `eta_pos` | 两个挂件的屏幕位置（`[x, y]`，拖动会自动保存） | `[2218, 570]` |
| `hotkeys` / `console.*` / `tail.*` / `focus.*` | 托盘 / 面板 / 终端 / 老板键的设置 | 见 `config.example.json` |

> 发薪日固定写死为**每月 10 号 18:00**（代码常量 `PAYDAY_DAY=10` / `PAYDAY_TIME=18:00`，在 `uptime/burn/__init__.py`）；要改发薪日需改代码重打包。

**节假日数据**：`data/holidays.json` 每年按国务院放假安排手动更新一次（含 `spans` / `off_days` / `extra_workdays` 补班）。倒计时/进度/补班识别都依赖它；数据年份不含当前年份时终端版 eta 会醒目提醒。文件已入库，clone 后直接用即可。

---

## 4. 源码运行

```
# 托盘壳（菜单里开各模块、面板；开机驻留）
py -3.10 -m uptime

# 只开某个挂件
py -3.10 -m uptime.burn
py -3.10 -m uptime.eta
```

测试钩子：

```
# 注入时间复现/截图（对 burn、eta 都生效）
UPTIME_FAKE_NOW="2026-09-03T09:00:00" py -3.10 -m uptime.burn
```

---

## 5. 打包成单个 exe（可选）

```
py -3.10 build_exe.py
```

产物 `dist/uptime.exe`（单文件，约 30 MB）。用法：

```
uptime.exe                 # 托盘壳（双击即可）
uptime.exe burn            # 直接开 burn 挂件
uptime.exe eta             # 直接开 eta 挂件
```

`data/`、`config.example.json` 已打进 exe；真实配置在 `%APPDATA%\uptime\config.json` 首跑自动生成。exe 可放任意位置（如桌面）。

---

## 6. 别人要"一样的效果"的清单

1. `git clone` 本仓库，`py -3.10 -m pip install -r requirements.txt`
2. `copy config.example.json config.json`，把 `monthly_salary` 改成自己的月薪，上下班/午休/工作日按自己情况改
3. 确认 `data/holidays.json` 的 `year` 等于当前年份（过期的年份要更新）
4. `py -3.10 -m uptime.burn` / `uptime.eta`（或按 §5 打包 exe）

对上了可以这样核验：

- burn 进度条在你**下次发薪日**归 0 并重新爬；倒计时到点前是秒级递减
- 若某月 10 号是周六/法定假，发薪终点会自动落到最近的前一个工作日 18:00
- 上班时段 burn 每秒弹一枚像素金币、每 5 秒抛一次美钞
- eta 的大倒计时与 `HOL:` 倒计时都**贴整秒**跳动；`HOL:` 归零时刻 = 放假前最后一个工作日的 18:00

---

## 7. 常见坑（不是 bug）

- Git Bash / 管道下直跑 exe 模块报 `UnicodeEncodeError`：rich 在非真终端下的渲染伪影；用 cmd/真实控制台或双击即可，不是打包问题。
- 无边框挂件位置记忆在 `widgets.<代号>_pos`，拖动松手自动写回配置。
- `config.json` 别提交（含月薪）。桌面只留 exe，配置零明文。
