---
sorting-spec: |-
  // 侧栏顺序（Custom File Explorer sorting 插件读这一页）
  // 根目录：主人看的 首页、汇报、进度、每日、会议、知识库 在上；agent 的 主线、结构、交接、实验索引、模板 在下（CSS 调淡）
  首页
  汇报
  进度
  每日
  会议
  知识库
  术语
  项目规则
  资源
  agent
  ...
  // 汇报：页名以日期开头，倒序 = 最新的在上
  target-folder: 汇报
  > a-z
  要批
  同步
  ...
  附件
  // 归档 放最后
  /! 归档
  // 同步：最近更新的在最上面
  target-folder: 汇报/同步
  > modified
  // 要批：页名以日期开头，最新的在上
  target-folder: 汇报/要批
  > a-z
---

这页只给侧栏排序插件读，不用看。改顺序前先告诉维护者。
