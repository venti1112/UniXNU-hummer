/*
 * hummer: CPU frequency control through the EPSS block (SM8750 "sun").
 *
 * The stock device tree describes `qcom,cpufreq-epss` at 0x17d91000 /
 * 0x17d92000 / 0x17d93000, and the CPUs are split across those three domains
 * (cpu@0-200 -> domain 0, cpu@300-600 -> domain 1, cpu@700 -> domain 2). The
 * register layout is the one drivers/cpufreq/qcom-cpufreq-hw.c uses for EPSS:
 *
 *   0x000  enable       bit0: the hardware owns this domain
 *   0x020  domain state  low 8 bits: the LMh-throttled level
 *   0x0b0  dcvs ctrl     bit0: per-core DCVS
 *   0x100  frequency LUT, 4 bytes per row
 *   0x200  voltage LUT,   4 bytes per row
 *   0x308  interrupt clear
 *   0x320  performance state: write the level index to request it
 *
 * A LUT row is (LUT_SRC [31:30], LUT_CORE_COUNT [18:16], LUT_VOLT [11:0],
 * LUT_L_VAL [7:0]); with LUT_SRC == 0 the frequency is xo_rate * L_VAL, so the
 * rows are printed raw and in MHz once the caller tells us the XO rate.
 *
 * Why this instead of the SCMI performance protocol the REDMI K90 port uses:
 * the SM8750 in this phone exposes EPSS directly, and the K90's SCMI shmem
 * (0x17b4e000) and CPUCP mailbox (0x17830000) are NOT the addresses this board
 * uses (0x17d09400 / 0x17400000). Writing the K90 addresses here would poke
 * unrelated registers. EPSS needs no mailbox at all.
 *
 * Boot arguments:
 *   cpufreq=2  report only: per domain the enable bit, the DCVS bit, every LUT
 *              row and the level it currently sits at
 *   cpufreq=3  additionally pin every domain to its highest usable level
 *              (the "performance" policy; no governor, no thermal feedback)
 * Anything else, or absent, leaves the machine exactly as it boots.
 */
#include <kern/clock.h>
#include <kern/sched_prim.h>
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <sys/systm.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);

#define EPSS_WINDOW        0x1000
#define EPSS_ENABLE        0x000
#define EPSS_DOMAIN_STATE  0x020
#define EPSS_DCVS_CTRL     0x0b0
#define EPSS_FREQ_LUT      0x100
#define EPSS_VOLT_LUT      0x200
#define EPSS_INTR_CLEAR    0x308
#define EPSS_PERF_STATE    0x320

#define LUT_SRC(v)         (((v) >> 30) & 0x3u)
#define LUT_CORE_COUNT(v)  (((v) >> 16) & 0x7u)
#define LUT_VOLT(v)        ((v) & 0xfffu)
#define LUT_L_VAL(v)       ((v) & 0xffu)
#define LUT_MAX_ENTRIES    40u
#define LUT_ROW_BYTES      4u
#define LUT_TURBO_IND      1u

#define EPSS_DOMAINS       3

struct hummer_epss_domain {
	const char *name;
	vm_offset_t phys;
	volatile uint32_t *regs;
	uint32_t present;
	uint32_t levels;                    /* usable rows, turbo excluded */
	uint32_t top;                       /* index of the highest usable row */
	uint32_t empty_rows;                /* rows that read as zero, in order */
};

static struct hummer_epss_domain epss[EPSS_DOMAINS] = {
	{ "domain0", 0x17d91000UL, NULL, 0, 0, 0, 0 },
	{ "domain1", 0x17d92000UL, NULL, 0, 0, 0, 0 },
	{ "domain2", 0x17d93000UL, NULL, 0, 0, 0, 0 },
};

static uint32_t
epss_read(struct hummer_epss_domain *d, uint32_t offset)
{
	return d->regs[offset / 4];
}

static void
epss_write(struct hummer_epss_domain *d, uint32_t offset, uint32_t value)
{
	d->regs[offset / 4] = value;
	__builtin_arm_dsb(0xf);
}

/*
 * Walk the LUT. A row whose core count equals LUT_TURBO_IND is flagged turbo by
 * the Linux driver and must not be requested; a zero row ends the table. We
 * keep the highest usable index because that is where "performance" parks.
 */
static void
epss_describe(struct hummer_epss_domain *d, boolean_t verbose)
{
	uint32_t empty = 0;

	for (uint32_t i = 0; i < LUT_MAX_ENTRIES; i++) {
		uint32_t f = epss_read(d, EPSS_FREQ_LUT + i * LUT_ROW_BYTES);
		uint32_t v = epss_read(d, EPSS_VOLT_LUT + i * LUT_ROW_BYTES);
		uint32_t cores;

		if (f == 0 && v == 0) {
			empty++;
			continue;
		}
		cores = LUT_CORE_COUNT(f);
		if (verbose) {
			printf("hummer-cpufreq: %s level %2u src=%u l=%u cores=%u volt=%u\n",
			    d->name, i, LUT_SRC(f), LUT_L_VAL(f), cores, LUT_VOLT(v));
		}
		if (cores == LUT_TURBO_IND) {
			continue;
		}
		d->levels++;
		d->top = i;
	}
	d->empty_rows = empty;
}

static void
epss_report(struct hummer_epss_domain *d)
{
	uint32_t enable = epss_read(d, EPSS_ENABLE);
	uint32_t dcvs = epss_read(d, EPSS_DCVS_CTRL);
	uint32_t state = epss_read(d, EPSS_PERF_STATE);
	uint32_t lmh = epss_read(d, EPSS_DOMAIN_STATE) & 0xffu;

	printf("hummer-cpufreq: %s enable=%#x(dcvs=%#x) perf_state=%u top=%u levels=%u lmh=%u\n",
	    d->name, enable, dcvs, state, d->top, d->levels, lmh);
}

void
hummer_cpufreq_init(void)
{
	uint32_t mode = 0;
	boolean_t verbose;
	boolean_t pin;

	if (!PE_parse_boot_argn("cpufreq", &mode, sizeof(mode))) {
		return;
	}
	if (mode != 2 && mode != 3) {
		return;
	}
	verbose = mode == 2;
	pin = mode == 3;

	printf("hummer-cpufreq: EPSS init mode=%u\n", mode);
	for (uint32_t i = 0; i < EPSS_DOMAINS; i++) {
		struct hummer_epss_domain *d = &epss[i];

		d->regs = (volatile uint32_t *)ml_io_map(d->phys, EPSS_WINDOW);
		if (d->regs == NULL) {
			printf("hummer-cpufreq: %s map failed\n", d->name);
			continue;
		}
		/* The enable bit is documented as "the hardware owns this domain";
		 * a domain that reads zero everywhere is simply not wired on this
		 * board and is skipped rather than written to. */
		d->present = (epss_read(d, EPSS_ENABLE) & 0x1u) != 0;
		epss_describe(d, verbose);
		epss_report(d);
	}
	if (!pin) {
		return;
	}
	for (uint32_t i = 0; i < EPSS_DOMAINS; i++) {
		struct hummer_epss_domain *d = &epss[i];

		if (d->regs == NULL || d->levels == 0) {
			continue;
		}
		epss_write(d, EPSS_PERF_STATE, d->top);
		printf("hummer-cpufreq: %s pinned to level %u (perf_state now %u)\n",
		    d->name, d->top, epss_read(d, EPSS_PERF_STATE));
	}
	printf("hummer-cpufreq: done\n");
}
