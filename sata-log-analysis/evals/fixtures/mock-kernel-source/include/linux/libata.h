/* SPDX-License-Identifier: GPL-2.0 */
/* 模拟 include/linux/libata.h —— 摘录 AC_ERR_* 定义（评估用迷你源码树） */
#ifndef __LINUX_LIBATA_H__
#define __LINUX_LIBATA_H__

/* libata 错误类别掩码，对应日志中的 Emask 字段 */
enum {
	AC_ERR_DEV		= (1 << 0), /* 设备返回错误 */
	AC_ERR_HSM		= (1 << 1), /* 主机状态机违规 */
	AC_ERR_TIMEOUT		= (1 << 2), /* 命令超时 */
	AC_ERR_MEDIA		= (1 << 3), /* 介质错误 */
	AC_ERR_ATA_BUS		= (1 << 4), /* ATA 总线错误 */
	AC_ERR_HOST_BUS		= (1 << 5), /* 主机总线错误 */
	AC_ERR_SYSTEM		= (1 << 6), /* 系统错误 */
	AC_ERR_INVALID		= (1 << 7), /* 无效参数 */
	AC_ERR_OTHER		= (1 << 8), /* 其他 */
	AC_ERR_NODEV_HINT	= (1 << 9), /* 设备可能已消失 */
	AC_ERR_NCQ		= (1 << 10), /* NCQ 错误 */
};

/* EH action 标志，对应 exception 行 action 字段 */
enum {
	ATA_EH_REVALIDATE	= (1 << 0),
	ATA_EH_RESET		= (1 << 1),
	ATA_EH_PM_RESET		= (1 << 2),
	ATA_EH_PARK		= (1 << 3),
};

/* port 标志 */
enum {
	ATA_PFLAG_FROZEN	= (1 << 4), /* port 被冻结，待 EH 处理 */
	ATA_PFLAG_EH_PENDING	= (1 << 5),
};

#endif /* __LINUX_LIBATA_H__ */
