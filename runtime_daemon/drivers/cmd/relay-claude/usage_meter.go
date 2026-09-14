package main

// usageSnapshot is one turn's token + cost figures plus a shape tag. All fields
// are comparable, so snapshots can be compared with ==.
type usageSnapshot struct {
	input          int
	output         int
	cacheCreation  int
	cacheRead      int
	costUSD        float64
	fromModelUsage bool // derived from the modelUsage aggregate vs the bare usage field
}

// usageMeter converts a raw usage snapshot (which may be process-cumulative)
// into the per-turn usage to report. Implementations may carry cross-turn state.
type usageMeter interface {
	perTurn(cur usageSnapshot) usageSnapshot
}

// identityMeter passes the snapshot through unchanged. Used by V1 (fresh process
// per request), where the
// raw usage is already the per-turn figure.
type identityMeter struct{}

func (identityMeter) perTurn(cur usageSnapshot) usageSnapshot { return cur }
