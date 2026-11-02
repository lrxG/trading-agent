# S/E 插件落点（平台级技能目录）

WorkBuddy **项目级技能**目录：放进 `<id>/SKILL.md` 的能力会被平台识别、列入可用技能、按 `description` 自动触发。

- 落点判据：项目根 `skills/` **不被平台识别**（只是普通文件夹，只能靠指针+主动读）；本目录才进平台技能清单。
- 本目录属**本项目源码**，已在 `.gitignore` 里显式放行（`.workbuddy/*` 其余内容仍不进版本库），随 git 同步双机。
- 一个能力 = 一个子目录：`<id>/SKILL.md`（+ 其自身数据/脚本，按 0.18 插入流程）。

## 与注册表的关系

| 是什么 | 在哪 | 谁读 |
| --- | --- | --- |
| **能力登记**（唯一真源，调度/路由/审计都读它） | `data/state/capability_registry.json` | `tools/fs/registry.py` |
| **能力本体**（方法/流程/schema/接口） | 本目录 `<id>/SKILL.md` | LLM 按需加载 |
| **能力知识库**（实证数据） | `data/knowledge/<id>/` | 运行期只读，C6 维护期更新 |

插入新能力 3 步（0.18）：①在本目录建 `<id>/SKILL.md` + 定输出落点 ②`registry.py add` 追加一行 ③`registry.py verify` 自检 —— **全程不碰骨架文件**。
