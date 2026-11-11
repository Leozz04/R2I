# 架构

中文 | [English](ARCHITECTURE.md)

[总览](../README.zh-CN.md#怎么运转)背后的几块，每块一张图。命令见 [REFERENCE.md](REFERENCE.md)。

## 调度者、台账和你看到的

一个调度者会话开线程、派活；线程写 sqlite 台账；程序每分钟把台账渲染成 vault 页面；你的勾选和回答回到台账。

![调度者派线程，线程写台账，台账渲染成 vault 页面，负责人在页面里读和勾选](img/b1-manager-pipeline.svg)

![三层：负责人看的页面、作为唯一记录的台账、写台账的 agent](img/b2-manager-three-bands.svg)

![负责人的三个入口：要批页、报告、早报都由台账出；勾选、回答、新目标都回到台账](img/b3-manager-human-channels.svg)

## 要批闸门

只有「我来定」清单上的事才开要批页（`r2i ask`），其余由线程自己定，留一行记录。

![要批闸门：负责人清单上的事开要批页，其余线程自己定并记录](img/d1-approval-gate.svg)

## 花钱闸门

每笔付费调用都走 `r2i paid-run`：预算、单次上限、钥匙只从钥匙串取、花费记进台账。

![花钱闸门：每笔付费调用过预算和上限，钥匙从钥匙串取，花费写进台账](img/d4-paid-run-gate.svg)

## 夜间整理

`r2i nightly run` 读当天的线程和台账，写记忆建议、仓库整理方案、术语页和早报。

![夜间整理：线程日志和台账进去，出记忆建议、仓库整理方案、术语和早报](img/d2-nightly-consolidation.svg)

## 交接

到线时线程重写交接页；交接检查通过后新会话接手，旧会话停。

![交接：到线写交接，过检查，新会话接手，旧会话停](img/d3-handover.svg)

## 规则只有一份正本

规则在台账里，渲染成一份必带清单（约 3 KB）；`r2i entries` 从它生成 CLAUDE.md、AGENTS.md、GEMINI.md，任何会话都从同一套规则开工。

![规则单一正本：台账渲染出必带清单，再生成 CLAUDE.md、AGENTS.md、GEMINI.md 给任何会话](img/d5-rules-single-source.svg)
