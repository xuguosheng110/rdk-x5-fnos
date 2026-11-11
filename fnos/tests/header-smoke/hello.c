#include <linux/init.h>
#include <linux/module.h>

static int __init header_smoke_init(void)
{
	return 0;
}

static void __exit header_smoke_exit(void)
{
}

module_init(header_smoke_init);
module_exit(header_smoke_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("RDK X5 packaged header smoke test");

