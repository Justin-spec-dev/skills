// SPDX-License-Identifier: GPL-2.0
/* 模拟 drivers/ata/libata-core.c —— 摘录超时路径（评估用迷你源码树） */
#include <linux/libata.h>

/* ata_qc_timeout - 命令超时回调
 * qc 超过 deadline（默认 ATA_QC_TIMEOUT = 30s）未完成时由定时器触发。
 * 记录 AC_ERR_TIMEOUT（Emask 0x4），随后走 ata_port_freeze 冻结 port。
 */
static void ata_qc_timeout(struct ata_queued_cmd *qc)
{
	struct ata_port *ap = qc->ap;

	qc->err_mask |= AC_ERR_TIMEOUT;   /* 对应 Emask 0x4 (timeout) */
	qc->flags |= ATA_QCFLAG_FAILED;

	/* 超时路径冻结 port（区别于错误中断路径的 abort） */
	ata_port_freeze(ap);
}

/* ata_dev_revalidate - reset 成功后重新验证设备参数
 * 重新读 IDENTIFY，容量/特性变化则更新；失败打印
 * "revalidation failed (errno=%d)" 并摘除设备。
 */
static int ata_dev_revalidate(struct ata_device *dev, unsigned int new_class)
{
	/* ... */
}
