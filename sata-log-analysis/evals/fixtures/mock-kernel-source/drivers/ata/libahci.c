// SPDX-License-Identifier: GPL-2.0
/* 模拟 drivers/ata/libahci.c —— 摘录 AHCI hardreset（评估用迷你源码树） */
#include <linux/libata.h>

/* ahci_hardreset - AHCI 硬复位
 * 由 ata_eh_reset 在打印 "hard resetting link" 后调用。
 * 先 ahci_stop_engine 停 DMA，再调 sata_link_hardreset 发 COMRESET，
 * 最后重新使能 port。返回 0 表示链路重建且设备在线。
 */
int ahci_hardreset(struct ata_link *link, unsigned int *class,
		   unsigned long deadline)
{
	struct ata_port *ap = link->ap;
	bool online;
	int rc;

	ahci_stop_engine(ap);

	rc = sata_link_hardreset(link, sata_ehc_deb_timing(&link->eh_context),
				 deadline, &online, NULL);

	ahci_start_engine(ap);

	if (online)
		*class = ahci_dev_classify(ap);

	return rc;
}

/* ahci_error_handler - AHCI 的 EH 入口，挂到 scsi_host_template */
void ahci_error_handler(struct Scsi_Host *host)
{
	ata_scsi_error(host);
}
