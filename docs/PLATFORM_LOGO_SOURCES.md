# StoryForge 平台 Logo 官方来源清单

核验日期：2026-07-24  
用途：为 StoryForge 的平台固定资料提供可追溯的品牌身份和 Logo 参考，不作为商标授权证明。

## 使用原则

1. 优先使用平台方直接提供的透明 PNG/SVG；其次使用官方 Google Play / Apple App Store 应用图标。
2. 不从第三方 Logo 聚合站、搜索结果缩略图或小说封面水印获取“官方 Logo”。
3. 商店页面可能更换图标。下载后应保存原始文件、来源 URL、取得日期和 SHA-256，不要在运行时热链商店 CDN。
4. 如果只能从已获授权的推广截图临时裁切，必须保留截图来源，后续在平台档案中替换为高清官方资产。
5. 平台 Logo 与小说封面是两个独立资产。不得把作者名、书名水印、出版社字样或封面人物误识别为平台 Logo。
6. 商标和应用图标通常受权利人保护；正式商业发布前应按合作平台的品牌规范确认可用范围。

## 九个平台

| StoryForge 平台名 | 官方应用商店来源 | 应用/开发者识别信息 | 建议内部文件名 |
| --- | --- | --- | --- |
| GoodNovel | [Google Play：GoodNovel](https://play.google.com/store/apps/details?id=com.read.goodnovel) | 包名 `com.read.goodnovel`；商店开发者 GoodNovel | `goodnovel.png` |
| MotoNovel | [Google Play：MotoNovel](https://play.google.com/store/apps/details?id=com.motonovel.reader) | 包名 `com.motonovel.reader`；商店开发者 Tromo Electronics | `motonovel.png` |
| Novel Master | [Google Play：NovelMaster](https://play.google.com/store/apps/details?id=com.master.novel) | 包名 `com.master.novel`；商店开发者 SUNGAI PTE.LTD | `novel-master.png` |
| PlotNovel | [Google Play：PlotNovel](https://play.google.com/store/apps/details?id=com.plot.novel_app) | 包名 `com.plot.novel_app` | `plotnovel.png` |
| MegaNovel | [Google Play：MegaNovel](https://play.google.com/store/apps/details?id=com.newreading.meganovel) | 包名 `com.newreading.meganovel`；商店开发者 GoodNovel | `meganovel.png` |
| NovelShort | [Google Play：NovelShort](https://play.google.com/store/apps/details?id=com.novelshort.book) | 包名 `com.novelshort.book`；商店开发者 BlackGemStone | `novelshort.png` |
| Novellia | [Google Play：Novellia](https://play.google.com/store/apps/details?id=com.fread.novelphoenixa) | 包名 `com.fread.novelphoenixa`；商店开发者 phoenix read-novellia | `novellia.png` |
| JoyRead | [Google Play：JoyRead](https://play.google.com/store/apps/details?id=com.whaledream.novel) | 包名 `com.whaledream.novel`；商店开发者 JoyRead | `joyread.png` |
| Novelly X | [Apple App Store：Novelly X](https://apps.apple.com/us/app/novelly-x/id6752803792) | App ID `6752803792`；开发者 Dovity Inc | `novelly-x.png` |

平台方社交账号可作为人工交叉核验来源：

- [GoodNovel 官方 TikTok](https://www.tiktok.com/@goodnovelofficial)
- [NovelMaster TikTok](https://www.tiktok.com/@novelmaster00)

## 进入 StoryForge 前的资产规范

- 推荐画布：正方形透明 PNG，至少 `256 × 256`，优先 `512 × 512` 或更高。
- Logo 四周保留约 8%–12% 安全留白，不要把图标贴边。
- 不把圆角卡片背景、商店截图文字、评分、认证徽章或搜索口令一起裁进 Logo。
- 保持原始色彩，不用小说题材色覆盖 Logo；StoryForge 的 `brand_color` 单独保存。
- 文件名只使用小写英文字母、数字和连字符。
- 上传平台档案后，由 Hub 保存到 `attachments/platform-assets` 并向制作电脑分发；业务记录应保存 Hub URI，不保存某台客户端的缓存绝对路径。

