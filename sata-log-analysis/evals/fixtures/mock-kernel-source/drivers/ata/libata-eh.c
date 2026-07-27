// SPDX-License-Identifier: GPL-2.0
/* 模拟 drivers/ata/libata-eh.c —— 摘录 EH 关键函数（评估用迷你源码树，
 * 基于主线 libata-eh.c 行为简化，函数名与打印格式保持一致） */
#include <linux/libata.h>

/* ata_port_freeze - 冻结 port，中止在飞命令
 * 超时路径调用：qc 超时后先冻结 port 再调度 EH。
 * 日志中 "frozen" 字样即本函数置 ATA_PFLAG_FROZEN 的结果。
 */
int ata_port_freeze(struct ata_port *ap)
{
	ap->pflags |= ATA_PFLAG_FROZEN;
	ata_eh_freeze_port(ap);   /* 中止所有在飞 qc */
	ata_port_schedule_eh(ap);
	return 1;
}

/* ata_eh_link_report - 打印 exception 行与失败命令信息
 * 日志 "ataX.YY: exception Emask 0x%x SAct 0x%x SErr 0x%x action 0x%x frozen"
 * 由本函数打印。action 为 ehc->i.action（ATA_EH_* 组合）；
 * 末尾 "frozen" 取决于 ap->pflags 是否含 ATA_PFLAG_FROZEN。
 */
static void ata_eh_link_report(struct ata_link *link)
{
	struct ata_port *ap = link->ap;
	struct ata_eh_context *ehc = &link->eh_context;
	const char *frozen;

	frozen = (ap->pflags & ATA_PFLAG_FROZEN) ? " frozen" : "";

	ata_link_warn(link,
		"exception Emask 0x%x SAct 0x%x SErr 0x%x action 0x%x%s\n",
		ehc->i.err_mask, readl(ap->ioaddr.scr_active),
		ehc->i.serror, ehc->i.action, frozen);

	/* 逐 qc 打印 failed command、cmd/res taskfile */
	ata_eh_report_qc(link);  /* "failed command: ..." / "cmd ..." / "res ..." */
}

/* ata_eh_reset - 执行 reset
 * 按 action 中的 ATA_EH_RESET 标志调用驱动的 softreset/hardreset。
 * softreset 失败且 hardreset 存在时升级为 hardreset；
 * 打印 "hard resetting link" 后调用 ops->hardreset。
 */
static int ata_eh_reset(struct ata_link *link, int classify,
			ata_prereset_fn_t prereset, ata_reset_fn_t softreset,
			ata_reset_fn_t hardreset, ata_postreset_fn_t postreset)
{
	ata_link_info(link, "hard resetting link\n");
	rc = hardreset(link, &classes, deadline);   /* 失败则升级/降速 */
	/* ... */
}

/* ata_do_eh - EH 主循环：autopsy -> report -> recover */
void ata_do_eh(struct ata_port *ap, ata_prereset_fn_t prereset,
	       ata_reset_fn_t softreset, ata_reset_fn_t hardreset,
	       ata_postreset_fn_t postreset)
{
	ata_eh_autopsy(ap);          /* 归类失败 qc 错误 */
	ata_eh_report(ap);           /* 打印 exception 行 */
	ata_eh_recover(ap);          /* reset + 重试 + 重验证 */
}

/* ata_eh_finish - EH 收尾
 * 清除 ATA_PFLAG_FROZEN，恢复 qc 下发，打印 "EH complete"。
 */
static void ata_eh_finish(struct ata_port *ap)
{
	ap->pflags &= ~ATA_PFLAG_FROZEN;
	ata_port_info(ap, "EH complete\n");
}
