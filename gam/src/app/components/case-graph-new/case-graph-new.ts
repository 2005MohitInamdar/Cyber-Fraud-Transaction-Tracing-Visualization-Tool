import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Input,
  OnDestroy,
  ViewChild,
  computed,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { CaseGraph, CaseGraphEdge, CaseGraphNode } from '../../services/case_Details/case-details';
import { Bounds, GraphView, Point, edgePath, fitView, formatCompactInr, nodeRadius, zoomAt } from '../case-graph-new/graph-math';

// ── Layout constants ─────────────────────────────────────────────────────────
const PADDING_X = 80;
const PADDING_Y = 76;
const COLUMN_GAP = 240;
const ROW_GAP = 100;
const MIN_ZOOM = 0.2;
const MAX_ZOOM = 4;
const FIT_PADDING = 40;

const ROLE_COLORS: Record<string, string> = {
  victim: '#f87171',
  intermediate: '#818cf8',
  end_of_trail: '#34d399',
  endOfTrail: '#34d399', // tolerate either spelling from the backend
  isolated: '#64748b',
};


// ── Types ────────────────────────────────────────────────────────────────────
export interface LayoutNode extends CaseGraphNode {
  x: number;
  y: number;
  radius: number;
}

export interface LayoutEdge extends CaseGraphEdge {
  sourceNode: LayoutNode;
  targetNode: LayoutNode;
}

export interface GraphLayout {
  width: number;
  height: number;
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  layers: number[];
}

interface TooltipState {
  x: number;
  y: number;
  title: string;
  lines: string[];
}

interface DragState {
  id: string;
  pointerId: number;
  start: Point;
  original: Point;
}

interface PanState {
  pointerId: number;
  start: Point;
  view: GraphView;
}


// ── Pure layout helpers (exported so they can be unit-tested) ────────────────

/** Attach the full source/target node objects to each edge, dropping dangling edges. */
export function linkEdges(edges: CaseGraphEdge[], nodesById: Map<string, LayoutNode>): LayoutEdge[] {
  const linked: LayoutEdge[] = [];
  for (const edge of edges) {
    const sourceNode = nodesById.get(edge.source);
    const targetNode = nodesById.get(edge.target);
    if (sourceNode && targetNode) {
      linked.push({ ...edge, sourceNode, targetNode });
    }
  }
  return linked;
}

/**
 * One column per layer. Inside a column, nodes are ordered by the average
 * y-position of their parents (so edges cross less); nodes without parents go last.
 */
export function computeLayout(graph: CaseGraph): GraphLayout {
  const layers = [...new Set(graph.nodes.map((n) => n.layer))].sort((a, b) => a - b);

  const parentsOf = new Map<string, string[]>();
  for (const edge of graph.edges) {
    parentsOf.set(edge.target, [...(parentsOf.get(edge.target) ?? []), edge.source]);
  }

  const placed = new Map<string, LayoutNode>();
  const nodes: LayoutNode[] = [];
  let tallestColumn = 0;

  const averageParentY = (node: CaseGraphNode): number => {
    const ys = (parentsOf.get(node.id) ?? [])
      .map((id) => placed.get(id)?.y)
      .filter((y): y is number => y !== undefined);
    return ys.length ? ys.reduce((sum, y) => sum + y, 0) / ys.length : Infinity;
  };

  layers.forEach((layer, column) => {
    const column_nodes = graph.nodes
      .filter((n) => n.layer === layer)
      .map((node) => ({ node, sortKey: averageParentY(node) }))
      .sort((a, b) => (a.sortKey === b.sortKey ? a.node.id.localeCompare(b.node.id) : a.sortKey - b.sortKey))
      .map((item) => item.node);

    tallestColumn = Math.max(tallestColumn, column_nodes.length);

    column_nodes.forEach((node, row) => {
      const laidOut: LayoutNode = {
        ...node,
        x: PADDING_X + column * COLUMN_GAP,
        y: PADDING_Y + row * ROW_GAP,
        radius: nodeRadius(node.disputedAmount),
      };
      placed.set(node.id, laidOut);
      nodes.push(laidOut);
    });
  });

  return {
    width: Math.max(420, PADDING_X * 2 + Math.max(0, layers.length - 1) * COLUMN_GAP + 140),
    height: Math.max(260, PADDING_Y + Math.max(1, tallestColumn) * ROW_GAP + 90),
    nodes,
    edges: linkEdges(graph.edges, placed),
    layers,
  };
}


@Component({
  selector: 'app-case-graph-new',
  standalone:true,
  imports: [CommonModule],
  templateUrl: './case-graph-new.html',
  styleUrls: ['./case-graph-new.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CaseGraphNew implements AfterViewInit, OnDestroy {
  @ViewChild('viewport') viewport?: ElementRef<HTMLDivElement>;
  @ViewChild('graphSvg') svg?: ElementRef<SVGSVGElement>;

  // The input. Getter and setter share one type, which fixes the TS2322 error.
  private current!: CaseGraph;

  @Input({ required: true })
  set graph(value: CaseGraph) {
    this.current = value;
    this.layout.set(computeLayout(value));
    this.positions.set({});
    this.needsFit = true;
    queueMicrotask(() => this.fitIfPending());
  }
  get graph(): CaseGraph {
    return this.current;
  }

  // ── State ──────────────────────────────────────────────────────────────────
  readonly layout = signal<GraphLayout>({ width: 420, height: 260, nodes: [], edges: [], layers: [] });
  readonly positions = signal<Record<string, Point>>({}); // user drag overrides
  readonly view = signal<GraphView>({ k: 1, tx: 0, ty: 0 });
  readonly size = signal({ width: 1, height: 560 });
  readonly hovered = signal<string | null>(null);
  readonly dragging = signal<DragState | null>(null);
  readonly panning = signal<PanState | null>(null);
  readonly tooltip = signal<TooltipState | null>(null);

  private observer?: ResizeObserver;
  private viewportOrigin: Point = { x: 0, y: 0 }; // viewport's top-left in client coords
  private needsFit = true;
  private activePointers = new Map<number, Point>();
  private pinchStart: { distance: number; midpoint: Point; view: GraphView } | null = null;
  private readonly wheelListener = (e: WheelEvent) => this.onWheel(e);

  // ── Derived state ──────────────────────────────────────────────────────────
  /** Layout positions with the user's drag overrides applied. */
  readonly nodes = computed<LayoutNode[]>(() => {
    const overrides = this.positions();
    return this.layout().nodes.map((n) => ({ ...n, ...(overrides[n.id] ?? {}) }));
  });

  readonly edges = computed<LayoutEdge[]>(() => {
    const byId = new Map(this.nodes().map((n) => [n.id, n]));
    return linkEdges(this.layout().edges, byId);
  });

  /** Same nodes, but the active one is last so it is drawn on top. */
  readonly drawnNodes = computed<LayoutNode[]>(() => {
    const activeId = this.activeId();
    return [...this.nodes()].sort((a, b) => Number(a.id === activeId) - Number(b.id === activeId));
  });

  readonly transform = computed(() => {
    const v = this.view();
    return `translate(${v.tx},${v.ty}) scale(${v.k})`;
  });

  // ── Template helpers ───────────────────────────────────────────────────────
  readonly trackNode = (_: number, n: LayoutNode) => n.id;
  readonly trackEdge = (_: number, e: LayoutEdge) => `${e.source}|${e.target}`;
  readonly trackLayer = (_: number, layer: number) => layer;
  readonly compact = formatCompactInr;

  color(n: LayoutNode): string {
    return ROLE_COLORS[n.role] ?? ROLE_COLORS['isolated'];
  }

  textColor(n: LayoutNode): string {
    return n.role === 'isolated' ? '#ffffff' : '#0f172a';
  }

  /** Font size that keeps the amount inside the circle (8px floor, 13px cap). */
  radiusText(n: LayoutNode): number {
    const chars = Math.max(formatCompactInr(n.disputedAmount).length, 3);
    return Math.max(8, Math.min(13, (n.radius * 1.6) / chars));
  }

  label(n: LayoutNode): string {
    const bank = n.bank || 'Unknown bank';
    return bank.length > 14 ? `${bank.slice(0, 14)}…` : bank;
  }

  path(e: LayoutEdge): string {
    return edgePath(e.sourceNode, e.sourceNode.radius, e.targetNode, e.targetNode.radius);
  }

  edgeWidth(e: LayoutEdge): number {
    return Math.min(6, Math.max(1, 1 + Math.sqrt(Math.max(e.amountPassed ?? 0, 0)) / 20));
  }

  amount(value: number | null | undefined): string {
    if (value == null) return '—';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR',
      maximumFractionDigits: 2,
    }).format(value);
  }

  /** The node whose neighbourhood is highlighted: the dragged one, else the hovered one. */
  private activeId(): string | null {
    return this.dragging()?.id ?? this.hovered();
  }

  dimNode(n: LayoutNode): boolean {
    const id = this.activeId();
    if (!id || n.id === id) return false;
    const isNeighbour = this.edges().some(
      (e) => (e.source === id && e.target === n.id) || (e.target === id && e.source === n.id),
    );
    return !isNeighbour;
  }

  dimEdge(e: LayoutEdge): boolean {
    const id = this.activeId();
    return !!id && e.source !== id && e.target !== id;
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────
  ngAfterViewInit(): void {
    // The component test intentionally creates it without a graph input.
    if (!this.viewport || !this.svg) return;
    this.observer = new ResizeObserver(() => {
      this.measure();
      this.fitIfPending();
    });
    this.observer.observe(this.viewport!.nativeElement);
    // Registered manually because Angular's (wheel) binding can't be non-passive.
    this.svg!.nativeElement.addEventListener('wheel', this.wheelListener, { passive: false });
    this.measure();
    this.fitIfPending();
  }

  ngOnDestroy(): void {
    this.observer?.disconnect();
    this.svg?.nativeElement.removeEventListener('wheel', this.wheelListener);
  }

  // ── Pointer helpers ────────────────────────────────────────────────────────
  /** Pointer position relative to the viewport's top-left corner. */
  private point(e: { clientX: number; clientY: number }): Point {
    return { x: e.clientX - this.viewportOrigin.x, y: e.clientY - this.viewportOrigin.y };
  }

  private capture(e: PointerEvent): void {
    this.svg?.nativeElement.setPointerCapture(e.pointerId);
  }

  private moveNode(id: string, position: Point): void {
    this.positions.update((all) => ({ ...all, [id]: position }));
  }

  // ── Pan ────────────────────────────────────────────────────────────────────
  startPan(e: PointerEvent): void {
    if (e.button !== 0) return;
    // Nodes stop propagation in startDrag, so reaching here means background or an edge.
    this.capture(e);
    this.panning.set({ pointerId: e.pointerId, start: this.point(e), view: this.view() });
    this.activePointers.set(e.pointerId, this.point(e));
    this.tooltip.set(null);
  }

  // ── Node drag ──────────────────────────────────────────────────────────────
  startDrag(e: PointerEvent, n: LayoutNode): void {
    if (e.button !== 0) return;
    e.stopPropagation(); // don't also start a pan
    this.capture(e);
    this.dragging.set({
      id: n.id,
      pointerId: e.pointerId,
      start: this.point(e),
      original: { x: n.x, y: n.y },
    });
    this.activePointers.set(e.pointerId, this.point(e));
    this.hovered.set(n.id);
    this.tooltip.set(null);
  }

  move(e: PointerEvent): void {
    const pointer = this.point(e);
    if (this.activePointers.has(e.pointerId)) this.activePointers.set(e.pointerId, pointer);

    const drag = this.dragging();
    if (drag?.pointerId === e.pointerId) {
      const k = this.view().k; // screen pixels -> graph units
      this.moveNode(drag.id, {
        x: drag.original.x + (pointer.x - drag.start.x) / k,
        y: drag.original.y + (pointer.y - drag.start.y) / k,
      });
      return;
    }

    const pan = this.panning();
    if (this.activePointers.size === 2 && !this.dragging()) {
      this.pinch();
      return;
    }
    if (pan?.pointerId === e.pointerId) {
      this.view.set({
        ...pan.view,
        tx: pan.view.tx + pointer.x - pan.start.x,
        ty: pan.view.ty + pointer.y - pan.start.y,
      });
    }
  }

  end(e: PointerEvent): void {
    this.activePointers.delete(e.pointerId);
    this.pinchStart = null;
    if (this.dragging()?.pointerId === e.pointerId) this.dragging.set(null);
    if (this.panning()?.pointerId === e.pointerId) this.panning.set(null);
  }

  private pinch(): void {
    const [first, second] = [...this.activePointers.values()];
    const distance = Math.hypot(second.x - first.x, second.y - first.y);
    const midpoint = { x: (first.x + second.x) / 2, y: (first.y + second.y) / 2 };
    if (!this.pinchStart) {
      this.pinchStart = { distance, midpoint, view: this.view() };
      this.tooltip.set(null);
      return;
    }
    this.view.set(zoomAt(
      this.pinchStart.view,
      distance / Math.max(1, this.pinchStart.distance),
      this.pinchStart.midpoint.x,
      this.pinchStart.midpoint.y,
      MIN_ZOOM,
      MAX_ZOOM,
    ));
  }

  // ── Zoom and fit ───────────────────────────────────────────────────────────
  onWheel(e: WheelEvent): void {
    if (!e.ctrlKey && !e.metaKey) return; // plain wheel keeps scrolling the page
    e.preventDefault();
    const p = this.point(e);
    this.view.update((v) => zoomAt(v, Math.exp(-e.deltaY * 0.0015), p.x, p.y, MIN_ZOOM, MAX_ZOOM));
    this.tooltip.set(null);
  }

  /** Zoom around the viewport centre (used by the +/− buttons and keys). */
  zoom(factor: number): void {
    const { width, height } = this.size();
    this.view.update((v) => zoomAt(v, factor, width / 2, height / 2, MIN_ZOOM, MAX_ZOOM));
    this.tooltip.set(null);
  }

  fit(): void {
    const nodes = this.nodes();
    if (!nodes.length) return;
    const bounds: Bounds = {
      minX: Math.min(...nodes.map((n) => n.x - n.radius)),
      maxX: Math.max(...nodes.map((n) => n.x + n.radius)),
      minY: Math.min(...nodes.map((n) => n.y - n.radius)),
      maxY: Math.max(...nodes.map((n) => n.y + n.radius)),
    };
    const { width, height } = this.size();
    this.view.set(fitView(bounds, width, height, FIT_PADDING));
    this.tooltip.set(null);
  }

  reset(): void {
    this.positions.set({});
    this.needsFit = true;
    queueMicrotask(() => this.fitIfPending());
  }

  /**
   * Fit only when a fit is pending (new graph or reset), so a window resize
   * doesn't throw away the user's current zoom and pan.
   */
  private fitIfPending(): void {
    if (!this.needsFit || !this.viewport) return;
    this.needsFit = false;
    this.measure();
    this.fit();
  }

  // ── Keyboard ───────────────────────────────────────────────────────────────
  nodeKey(e: KeyboardEvent, n: LayoutNode): void {
    const step = e.shiftKey ? 40 : 10;
    const moves: Record<string, Point> = {
      ArrowLeft: { x: -step, y: 0 },
      ArrowRight: { x: step, y: 0 },
      ArrowUp: { x: 0, y: -step },
      ArrowDown: { x: 0, y: step },
    };
    const delta = moves[e.key];
    if (!delta) return;
    e.preventDefault();
    e.stopPropagation();
    this.moveNode(n.id, { x: n.x + delta.x, y: n.y + delta.y });
  }

  viewportKey(e: KeyboardEvent): void {
    if (e.target !== e.currentTarget) return; // ignore keys bubbling up from nodes

    if (e.key === '+' || e.key === '=') {
      e.preventDefault();
      this.zoom(1.25);
      return;
    }
    if (e.key === '-') {
      e.preventDefault();
      this.zoom(0.8);
      return;
    }
    if (e.key === '0') {
      e.preventDefault();
      this.fit();
      return;
    }

    const pans: Record<string, Point> = {
      ArrowLeft: { x: 40, y: 0 },
      ArrowRight: { x: -40, y: 0 },
      ArrowUp: { x: 0, y: 40 },
      ArrowDown: { x: 0, y: -40 },
    };
    const delta = pans[e.key];
    if (delta) {
      e.preventDefault();
      this.view.update((v) => ({ ...v, tx: v.tx + delta.x, ty: v.ty + delta.y }));
    }
  }

  // ── Tooltip ────────────────────────────────────────────────────────────────
  show(e: PointerEvent | FocusEvent, n: LayoutNode): void {
    if (this.dragging() || this.panning()) return;
    this.hovered.set(n.id);

    const size = this.size();
    const pointer = e instanceof PointerEvent ? this.point(e) : { x: size.width / 2, y: 20 };

    const fields: Array<[string, string | null | undefined]> = [
      ['Account', n.accountNo],
      ['UTR / transaction ID', n.utr],
      ['Transaction amount', this.amount(n.txAmount)],
      ['Disputed amount', this.amount(n.disputedAmount)],
      ['Frozen amount', this.amount(n.frozenAmount)],
      ['Action taken by', n.actionTakenBy],
      ['Remarks', n.remarks],
    ];

    this.tooltip.set({
      x: Math.max(8, Math.min(pointer.x + 14, Math.max(8, size.width - 280))),
      y: Math.max(8, Math.min(pointer.y + 14, Math.max(8, size.height - 160))),
      title: n.bank || n.id,
      lines: fields.filter(([, value]) => value && value !== '—').map(([key, value]) => `${key}: ${value}`),
    });
  }

  clear(): void {
    if (this.dragging() || this.panning()) return;
    this.hovered.set(null);
    this.tooltip.set(null);
  }

  // ── Viewport measurement ───────────────────────────────────────────────────
  private measure(): void {
    const rect = this.viewport?.nativeElement.getBoundingClientRect();
    if (!rect) return;
    this.viewportOrigin = { x: rect.left, y: rect.top };
    this.size.set({ width: Math.max(1, rect.width), height: Math.max(1, rect.height) });
  }
}
