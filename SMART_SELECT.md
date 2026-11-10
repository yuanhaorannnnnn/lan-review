# 本机智能选物

智能选物是可选能力，使用本地 MobileSAM 对用户明确点击的图片或视频暂停帧计算候选轮廓。模型和输入不向外部服务发送；结果是候选，必须由用户确认或改用手动框选。它不识别物体名称，也不跟踪视频中的运动。

## 依赖与模型

项目随附 MobileSAM 源码 `vendor/mobilesam/mobile_sam/`、其 Apache-2.0 许可证 `vendor/mobilesam/LICENSE` 和官方权重 `models/mobile_sam.pt`（约 39 MiB）。来源：[MobileSAM 官方仓库](https://github.com/ChaoningZhang/MobileSAM)，源码与权重对应仓库提交 `f706ad9c4eb7f219c00d9050e46328518ffb65d2`。权重 SHA-256：`6dbb90523a35330fedd7f1d3dfc66f995213d81b29a5ca8108dbcdd4e37d6c2f`；加载前校验，不会运行时下载。

基础功能不依赖智能选物。若使用独立虚拟环境，在该环境安装适配本机的 PyTorch、TorchVision，以及 `timm`、`numpy`、`Pillow`、`opencv-python-headless`；GPU 版 PyTorch 应按 [PyTorch 官方安装说明](https://pytorch.org/get-started/locally/)选择与本机匹配的版本，不要盲目替换系统 CUDA 环境。本机已验证：Python 3.11、torch 2.7.0+cu126、torchvision 0.22.0、timm 1.0.15、numpy 1.26.4、Pillow 9.2.0、OpenCV 4.11、RTX 4090。其他环境尚未验收。

默认从项目 `models/mobile_sam.pt` 加载；可通过 `LANREVIEW_SMART_WEIGHTS` 设置同一官方权重的本地路径，模块仍校验上述哈希。服务重启后才使用新的环境变量。默认自动选择 CUDA；无 CUDA 时使用 CPU；单独调用模块可指定 `device='cpu'`、`'cuda'` 或 `'auto'`。本机单帧 CPU 调用产生了三个候选，首次完整调用约 1.81 秒；单次观测不能代表其他 CPU 或实际端到端性能。

## 结果与边界

`smart_select.segment_image(image_bytes, x, y)` 接收 PNG/JPEG/WebP 画面及归一化点坐标（0–1），返回最多三个 `{points, score}` 候选；`points` 为归一化 `[x,y]` 顶点，最多 256 个，`score` 为模型排序分数，不是正确率。当前为便于传输与持久化，仅保留每个 mask 最大连通区域的外轮廓并简化；孔洞、其他不相连部分不会保留，因此不应将其当作像素级精确分割。候选可能选中物体局部或大片背景，用户应确认并可回退到手动框选。

输入限制：图片最多 12 MiB、1200 万像素、单边 8192；模型大小不超过 100 MiB；每个服务进程同一时刻只处理一个请求，忙时明确返回错误。模型缺失、哈希不匹配、缺少依赖、画面无效和推理出错都返回 `SmartSelectError`，应用应提示可使用手动标注。限制不代替 HTTP 层对上传体大小、并发和允许网段的检查。

个人实验在割草机视频的一帧上，点击机器人中央时模型的最高分候选仅选中了白色圆片，另一候选才是整机；这说明候选排序不能替代用户确认。单帧实验不代表其他视频、硬件或真实交互的表现。
