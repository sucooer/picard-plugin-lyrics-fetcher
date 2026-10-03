# LRCLIB Lyrics

为 MusicBrainz Picard 3 从 [LRCLIB](https://lrclib.net) 抓取歌词的插件。

## 功能

- 把**带时间轴的歌词**写入 `syncedlyrics` 标签（Picard 3.0 起原生支持，会写成 MP3 的 SYLT 帧）
- 把**无时间轴的纯文本歌词**写入 `lyrics` 标签
- 可选：保存时在音乐文件旁导出一份 `.lrc` 文件
- 可选：已有歌词时不覆盖

## 安装

在 Picard 里打开 **选项 → 插件 → Install Plugin…**，选择 **本地仓库** 页签，
把本目录的路径填进去即可。

命令行方式：

```bash
picard-cli plugins install /path/to/picard-plugin-lrclib-lyrics
```

## 使用

安装后插件会出现在 **选项 → 插件** 列表里，自动启用，并在左侧选项树
「插件」节点下多出一个 **LRCLIB Lyrics** 设置页。

流程是全自动的：把音乐拖进 Picard → 插件按「标题 + 艺人 + 专辑 + 时长」
向 LRCLIB 查询 → 结果写进元数据面板 → **点保存才会写进文件**。

## 格式支持

| 格式 | `syncedlyrics`（带时间轴） | `lyrics`（纯文本） |
| --- | --- | --- |
| MP3 (ID3) | 支持 | 支持 |
| FLAC / OGG / Opus (Vorbis) | 支持 | 支持 |
| MP4 / M4A | **不支持** | 支持 |

## 注意事项

LRCLIB 是按**标题、艺人、专辑、时长**匹配的，不使用 MusicBrainz ID。
因此偶尔会匹配错或匹配不到。保存前建议扫一眼歌词。

繁简字形和艺人写法会影响匹配结果（例如同一首歌可能只收录了繁体版）。
抓不到时，先把文件里的 title / artist 改成更常见的写法再重试。

## 许可证

GPL-2.0-or-later，详见 `MANIFEST.toml`。
