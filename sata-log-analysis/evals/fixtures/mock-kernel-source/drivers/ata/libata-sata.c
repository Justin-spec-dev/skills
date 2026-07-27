// SPDX-License-Identifier: GPL-2.0
/* 模拟 drivers/ata/libata-sata.c —— 摘录链路状态与硬复位（评估用迷你源码树） */
#include <linux/libata.h>

/* sata_link_hardreset - 通过 COMRESET 复位链路
 * 写 SControl.DET=1 发起 COMRESET，等待 PHYRDY；
 * 超时未建立打印 "COMRESET failed (errno=%d)"。
 */
int sata_link_hardreset(struct ata_link *link, const unsigned long *timing,
			unsigned long deadline, bool *online,
			int (*check_ready)(struct ata_link *))
{
	/* ... COMRESET 序列 ... */
	if (rc)
		ata_link_err(link, "COMRESET failed (errno=%d)\n", rc);
	return rc;
}

/* sata_std_prereset - reset 前等待链路就绪
 * 等待 PHYRDY 期间打印
 * "link is slow to respond, please be patient (ready=%d)"。
 */
int sata_std_prereset(struct ata_link *link, unsigned long deadline)
{
	/* ... */
}

/* sata_print_link_status - 打印 "SATA link up X Gbps (SStatus %03x SControl %03x)"
 * 或 "SATA link down"。
 */
static void sata_print_link_status(struct ata_link *link)
{
	u32 sstatus, scontrol;

	if (sata_scr_read(link, SCR_STATUS, &sstatus))
		return;
	sata_scr_read(link, SCR_CONTROL, &scontrol);

	if (ata_link_online(link))
		ata_link_info(link, "SATA link up %s (SStatus %x SControl %x)\n",
			      sata_spd_string(sata_scr_speed(link)),
			      sstatus, scontrol);
	else
		ata_link_info(link, "SATA link down (SStatus %x SControl %x)\n",
			      sstatus, scontrol);
}
