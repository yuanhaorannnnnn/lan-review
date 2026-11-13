# HTTP API（首版）

所有响应为 JSON，除媒体文件外。允许网段内的成员可以查看列表、上传和评论；不开放跨域浏览器接入，异源 Origin 的写请求会被拒绝。本机路径读取和文件选择器仅供托管电脑本机调用。

- `POST /api/profile`：JSON `{name}` 更新当前浏览器的显示署名；空字符串恢复匿名。
- `POST /api/reviews/<id>/public-link`：由局域网创建指定评审外链（需配置 HTTPS 公网网关）；视频暂不支持。
- `GET /api/reviews`：返回 `{reviews: [...]}`。
- `POST /api/uploads` 创建上传任务，上传时通过 `X-Upload-Id` 关联；`POST /api/uploads/<id>/cancel` 请求取消，`GET /api/uploads/<id>` 查询清理状态。取消与提交有竞态时以服务端状态为准。
- `POST /api/reviews/<id>/delete`：从列表移除评审并使旧链接失效；若托管者启用定时清理，其他设备上传的文件和评论在 30 天后清理。
- `POST /api/reviews`：`multipart/form-data` 的 `file` 字段上传一个文件。Markdown 可用多个 `attachments` 字段显式上传图片，最多 20 张、合计 32 MiB；按相对引用的文件名匹配。返回 `{id, name, type, url}`。
- `POST /api/reviews/import`：仅托管电脑本机调用，JSON `{path}` 指定绝对路径；自动导入 Markdown 本地相对图片，返回创建信息和 `warnings` 字符串数组。
- `POST /api/reviews/pick-local`：仅托管电脑本机调用，打开本机文件选择器并导入选中的文件。
- `POST /api/markdown/images` 和 `POST /api/reviews/bundle`：解析上传的 Markdown 图片引用，并将其与显式上传的图片匹配；不会读取客户端路径或抓取远程图片。
- `POST /api/reviews/<id>/segment`：对图片或视频的上传画面按点生成本地智能选物候选；模型或依赖不可用时返回错误，可使用手动标注。
- `POST /api/reviews/<id>/complete`：记录当前浏览器已完成评审，重复调用不会重复产生动态；`GET /api/reviews/<id>` 的 `comments` 返回 `kind: "review_complete"` 动态，`completed_by_me` 表示本浏览器是否已完成。该标记不代表所有意见已解决。
- `POST /api/reviews/<id>/presence`：记录当前浏览器心跳，返回近 90 秒浏览该评审的浏览器数。
- `GET /api/reviews/<id>`：获取指定评审的元数据、`content_url` 和 `comments`，Markdown 附带 `rendered_url`。
- `GET /api/reviews/<id>/rendered`：Markdown 渲染结果 `{html, metadata}`，其中 `metadata` 是文档开头的 YAML 原文或 `null`。
- `GET /media/<id>/attachments/<attachment-id>`：获取该评审中显式上传的图片附件。
- `GET /media/<id>`：原文件；视频支持 HTTP Range。HTML 在 iframe 内预览，非 iframe 直接访问时下载原件；图片和视频按对应媒体类型返回。
- `POST /api/reviews/<id>/comments`：JSON `{author, body, anchor?, parent_id?, thumbnail?}`。锚点可为 `null`、文本 `{type:"text", quote, prefix?, suffix?}`、图片 `{type:"image", x, y}`（旧格式），或视频 `{type:"video", time, end_time?, region?}`。视频 `end_time` 为可选的结束时间，必须晚于开始时间；区域只对应开始帧。图片和视频可用 `region` 表示点、矩形或多边形，坐标均为 0–1 归一化值；视频时间以秒计。视频顶层评论可带经过限制的缩略图。回复指定顶层 `parent_id`，继承其定位。可提供 `Idempotency-Key` 请求头：同一评审中使用相同 key 和请求体重试时返回同一条评论；不同请求体复用 key 会被拒绝。

允许访问的 IP 和 Host 由服务启动参数约束；这些约束不构成联网身份认证。以实际服务行为和测试为准。

### 评审人进度

`GET /api/reviews/<id>` 返回 `participants` 数组：`id`、`name`、`completed`、`is_mine`。空数组表示未设置名单，不代表全部完成。

`POST /api/reviews/<id>/participants` 接收 `{"names":["小王","小李"]}`，替换名单；保留同名成员的完成状态，移除的成员及其名单完成状态被清除。名单最多 50 人，不允许重名。局域网用户及持有效外链的用户均可编辑名单。

`POST /api/reviews/<id>/complete` 在有名单时传 `{"participant_id":123}` 认领本人并标记完成；同一浏览器在同一名单中只能认领一人，已认领的名额不能由其他浏览器认领。没有名单时仍可直接调用。浏览器身份依赖 cookie，清除 cookie 或更换设备后不能自动识别身份；名单认领是协作约定，不是身份验证。

`POST /api/reviews/<id>/uncomplete` 撤销当前浏览器的完成状态（幂等），保留名单认领。原完成动态改为 `review_complete_history`，新增 `review_uncomplete` 动态；再次完成会新增 `review_complete`。名单项的 `claimed` 表示已被认领，`completed` 表示当前是否完成，两者不同。

当前浏览器标记完成后，新评论和回复返回 HTTP 409；先调用 `uncomplete` 才能继续。已成功提交的幂等请求仍可重试读取结果；其他评审人不受影响。
