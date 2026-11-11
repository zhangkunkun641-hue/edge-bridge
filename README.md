# Edge Browser Bridge

## Summary (English)

edge-bridge is a small, single-file Python tool that lets a remote AI assistant fully operate a Microsoft Edge browser running on your own Windows PC. It talks to Edge locally over the Chrome DevTools Protocol (CDP, bound to 127.0.0.1 only) and exposes a token-authenticated HTTP API that listens exclusively on your Tailscale interface, so the assistant can reach it only through your private Tailscale network — ideally further restricted by a Tailscale ACL that allows just the assistant's device. Security model: the CDP debug port never leaves localhost, every API call (except a minimal health check) requires a random per-install token, and by design the bridge has **no endpoint that exports cookies or other stored credentials** — it can operate pages, but it cannot hand out your logins.

---

一个跑在你自己电脑上的小工具：让助手通过 Tailscale 远程接管一个 Edge 浏览器。提供两种模式：一个与日常浏览器完全分开的**独立小号**，或你的**日常 Edge 本人**（带全部登录态）。能看页面、点按钮、填表单、截图、滚动、按键、上传/下载文件，还带一个原始 CDP 透传口子，等于把浏览器的方向盘整个交出去——所以防护也做在前面：token 鉴权、只绑 Tailscale 网卡、调试口只绑本机。

## 前置条件

1. **Python 3**：去 [python.org](https://www.python.org/downloads/) 下载安装，安装时**勾选 "Add python.exe to PATH"**。
2. **Tailscale**：已安装、已登录，并且和助手的设备在同一个 Tailscale 网络里。
3. **装依赖**：在文件资源管理器里打开本文件夹，在地址栏输入 `cmd` 回车，然后运行：

   ```
   pip install -r requirements.txt
   ```

   （只需要装一次。）

## 两个启动脚本，先分清用哪个

| | `start-edge.bat` | `start-edge-main.bat` |
| --- | --- | --- |
| 启动的是 | 独立小号 Edge | 你日常用的 Edge 本人 |
| 配置目录 | 单独目录（`%LOCALAPPDATA%\MuseEdgeProfile`） | Edge 的真实配置目录 |
| 登录态 | 只有你在小号里自己登的账号 | 你平时全部的登录态 |
| 适合 | 试水、低风险的杂活 | 确实需要助手接管日常浏览器时 |
| 注意事项 | 无，和平时浏览器互不影响 | **运行前必须彻底退出 Edge**（含任务栏托盘后台），用完正常关掉重开即恢复普通模式 |

两个脚本都带 `--remote-debugging-address=127.0.0.1`，调试口只绑本机，外面连不进来。

## 启动（顺序别反）

1. **双击你选的那个启动脚本**（小号用 `start-edge.bat`，日常浏览器用 `start-edge-main.bat`）。
2. **再运行桥**：在本文件夹打开命令行，运行：

   ```
   python bridge.py
   ```

3. **把地址和 token 发给助手**
   桥启动后，窗口里会打印类似这样的两行，直接复制发给助手：

   ```
   连接地址: http://<你的 Tailscale IP>:8765
   Token:    <一长串随机字符>
   ```

   如果打印的是 `127.0.0.1` 并有警告，说明 Tailscale 没连上，先把 Tailscale 打开登录再重启桥。

## 建议：给 Tailscale 加一条访问规则（ACL）

默认情况下，同一个 Tailscale 网络里的设备能互相访问。为了只放行助手 → 你这台电脑的桥端口，去 [Tailscale 管理后台](https://login.tailscale.com/admin/acls) 的 Access controls 里参考下面写（先在后台的 Machines 页面给你的电脑打标签 `tag:pc`、给助手的设备打标签 `tag:muse`）：

```json
{
  "tagOwners": {
    "tag:pc": ["autogroup:admin"],
    "tag:muse": ["autogroup:admin"]
  },
  "acls": [
    { "action": "accept", "src": ["tag:muse"], "dst": ["tag:pc:8765"] }
  ]
}
```

这一条的意思是：只有带 `tag:muse` 的设备能连你电脑的 8765 端口，其他一概不通。注意：如果你的 ACL 里本来就有其他规则，请把这条合并进去，别整段覆盖掉原有配置；拿不准就让助手先看一眼现有的再改。

## 如何停止

- 关掉运行 `bridge.py` 的那个黑色窗口（或在窗口里按 `Ctrl+C`）。
- 关掉用启动脚本打开的那个 Edge 的所有窗口（日常模式下记得再正常打开一次 Edge，就回到普通模式了）。

两个都关掉，助手就完全连不上了。

## 接口清单（API）

除 `GET /health`（健康检查，免 token，只回答桥活着没、调试口通不通）外，所有接口都是 `POST`、请求体为 JSON、且必须带请求头 `X-Bridge-Token: <你的 token>`。除注明外，`tabId` 可省略，省略时默认作用于第一个标签页。

| 接口 | 参数 | 说明 |
| --- | --- | --- |
| `/tabs` | — | 列出全部标签页（id、标题、地址） |
| `/open` | `url` | 新开标签页并打开网址 |
| `/text` | `tabId?` | 返回页面正文纯文本 |
| `/click` | `selector` | 点击 CSS 选择器命中的元素 |
| `/type` | `selector`, `text`, `submit?` | 给输入框填字；`submit` 为 true 时填完按回车 |
| `/eval` | `expression` | 在页面里执行一段 JS 并返回结果 |
| `/screenshot` | `tabId?` | 截图，返回 base64 编码的 PNG |
| `/scroll` | `x`, `y` | 滚动页面（像素数，y 正数向下、x 正数向右） |
| `/key` | `key`, `modifiers?` | 按一个键：`Enter`、`Tab`、`Escape`、方向键、`Backspace`、`F1`–`F12` 或单个字符；`modifiers` 可传整数位掩码（Alt=1、Ctrl=2、Meta=4、Shift=8）或名字数组如 `["ctrl","shift"]` |
| `/back`、`/forward`、`/reload` | `tabId?` | 后退 / 前进 / 刷新 |
| `/wait` | `selector`, `timeoutMs?` | 等某个元素出现（默认最多等 10 秒），超时返回明确错误 |
| `/upload` | `selector`, `filePath` | 把你电脑上的文件塞进页面的文件选择框 |
| `/download_dir` | `path` | 把浏览器下载目录设为你电脑上的指定目录 |
| `/close` | `tabId` | 关闭指定标签页 |
| `/cdp` | `method`, `params?`, `tabId?` | **原始 CDP 透传**，见下面专门说明 |

调用示例（PowerShell，把地址和 token 换成桥窗口里打印的那两行）：

```powershell
$h = @{ "X-Bridge-Token" = "<你的 token>" }
Invoke-RestMethod -Method Post -Uri "http://<你的 Tailscale IP>:8765/text" -Headers $h -Body "{}" -ContentType "application/json"
```

### /cdp 透传：能力与风险

`/cdp` 把 Chrome DevTools Protocol 原封不动地递给调用方：在指定标签页上执行任意 CDP 方法（`Page.reload`、`DOM.getDocument`、`Network.enable`、`Runtime.evaluate` ……）。它意味着：

- **能力上**：桥没有单独封装的动作，几乎都能经 /cdp 完成，它是“全盘接管”的核心口子。
- **风险上**：握有 token 的一方等于握有浏览器调试协议本身，可以在你已登录的页面里以你的身份做任何页面层面的事。所以：token 只发给助手本人、桥只绑 Tailscale、再配 ACL 三重收口，缺一不可；不用时就把桥关掉。

## 安全说明

- **调试口只绑本机**：Edge 的调试口（9222）只监听 `127.0.0.1`，局域网和互联网都连不进来，只有本机的桥能用它。
- **桥只绑 Tailscale 网卡**：桥只在你的 Tailscale 地址上对外服务，不会暴露到普通局域网；再配上上面的 ACL，就只有助手的设备能访问。找不到 Tailscale 地址时桥只绑 `127.0.0.1` 并打印警告，不会图省事绑到所有网卡上。
- **token 校验**：除健康检查外，每个请求都要带 token。token 在第一次运行时随机生成、存在本文件夹的 `token.txt` 里。别把 token 发给助手以外的人；万一泄露，删掉 `token.txt` 再重启桥，就会生成一个新的。
- **不导出 cookie（刻意的安全边界）**：桥没有任何读取/导出 cookie 的接口，也不会提供 `Network.getCookies` 这类现成口子。桥的定位是**操作页面**，不是**交出登录凭据**——助手能在你登录好的页面里干活，但拿不到、也不该拿到可以搬走另用的 cookie。日常模式下的 `/cdp` 理论上能触及调试协议的方方面面，因此更要靠上面的三重收口把“谁能调用”管死，而不是靠阉割功能。
- **最小暴露**：不用的时候就把桥和那个 Edge 关掉。日常上网和重要账号，优先用小号模式；非要接管日常浏览器时才用 `start-edge-main.bat`，且用完恢复普通模式。
