// ── Types ────────────────────────────────────────────────────────────────────
export interface Point {
  x: number;
  y: number;
}

/** Pan/zoom state: screen = graph * k + (tx, ty). */
export interface GraphView {
  k: number;
  tx: number;
  ty: number;
}

export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

// ── Numbers ──────────────────────────────────────────────────────────────────
export function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

const roundToOneDecimal = (n: number): number => Math.round(n * 10) / 10;

/**
 * Indian-style compact money for text inside circles:
 *   null -> "—", 500 -> "₹500", 45000 -> "₹45K", 123456 -> "₹1.2L", 12345678 -> "₹1.2Cr"
 */
export function formatCompactInr(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';

  const amount = Math.abs(value);
  const sign = value < 0 ? '-' : '';

  if (amount < 1_000) return `${sign}₹${Math.round(amount)}`;

  // `limit` is the point where the next tier reads better (100K -> 1L, 100L -> 1Cr).
  const tiers = [
    { divisor: 1_000, suffix: 'K', limit: 100 },
    { divisor: 100_000, suffix: 'L', limit: 100 },
    { divisor: 10_000_000, suffix: 'Cr', limit: Infinity },
  ];

  for (const { divisor, suffix, limit } of tiers) {
    const scaled = roundToOneDecimal(amount / divisor);
    if (scaled < limit) return `${sign}₹${scaled}${suffix}`; // 3 -> "3", 1.2 -> "1.2"
  }
  return `${sign}₹${amount}`; // unreachable, keeps TypeScript happy
}

/** Circle radius grows slowly with the disputed amount: 24px minimum, 40px maximum. */
export function nodeRadius(disputedAmount: number | null): number {
  const amount = Math.max(disputedAmount ?? 0, 0);
  return clamp(24 + Math.sqrt(amount) / 10, 24, 40);
}

// ── Coordinate transforms ────────────────────────────────────────────────────
/** Convert a point in the viewport (pixels) to graph coordinates. */
export function screenToGraph(view: GraphView, screenX: number, screenY: number): Point {
  return {
    x: (screenX - view.tx) / view.k,
    y: (screenY - view.ty) / view.k,
  };
}

/** Zoom by `factor` while keeping the graph point under (anchorX, anchorY) fixed on screen. */
export function zoomAt(
  view: GraphView,
  factor: number,
  anchorX: number,
  anchorY: number,
  minK: number,
  maxK: number,
): GraphView {
  const k = clamp(view.k * factor, minK, maxK);
  const anchor = screenToGraph(view, anchorX, anchorY); // graph point to keep still
  return {
    k,
    tx: anchorX - anchor.x * k,
    ty: anchorY - anchor.y * k,
  };
}

/**
 * Scale and position the view so `bounds` fits inside the viewport with `padding`,
 * centred. Never zooms in past 1:1, so a tiny graph isn't blown up.
 */
export function fitView(bounds: Bounds, viewportW: number, viewportH: number, padding: number): GraphView {
  const contentW = Math.max(1, bounds.maxX - bounds.minX);
  const contentH = Math.max(1, bounds.maxY - bounds.minY);
  const availableW = Math.max(1, viewportW - padding * 2);
  const availableH = Math.max(1, viewportH - padding * 2);

  const k = Math.min(1, availableW / contentW, availableH / contentH);

  const centerX = (bounds.minX + bounds.maxX) / 2;
  const centerY = (bounds.minY + bounds.maxY) / 2;

  return {
    k,
    tx: viewportW / 2 - centerX * k,
    ty: viewportH / 2 - centerY * k,
  };
}

// ── Edges ────────────────────────────────────────────────────────────────────
/**
 * SVG path for a curved edge between two circles. It starts and ends exactly on the
 * circle borders (along the line between the centres) and works for any direction,
 * including a child dragged to the left of its parent.
 */
export function edgePath(source: Point, sourceRadius: number, target: Point, targetRadius: number): string {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.hypot(dx, dy) || 1; // avoid dividing by zero if centres coincide

  const start: Point = {
    x: source.x + (dx / distance) * sourceRadius,
    y: source.y + (dy / distance) * sourceRadius,
  };
  const end: Point = {
    x: target.x - (dx / distance) * targetRadius,
    y: target.y - (dy / distance) * targetRadius,
  };

  // Horizontal handle length, pointing the way the edge travels.
  const direction = Math.sign(end.x - start.x) || 1;
  const handle = Math.max(40, Math.abs(end.x - start.x) / 2) * direction;

  return (
    `M ${start.x},${start.y} ` +
    `C ${start.x + handle},${start.y} ${end.x - handle},${end.y} ${end.x},${end.y}`
  );
}
