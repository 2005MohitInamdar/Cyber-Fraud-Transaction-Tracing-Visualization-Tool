import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { IncompleteItem } from '../../services/case_Details/case-details';

export interface IncompleteGroup { key: string; title: string; severity: 'high' | 'medium' | 'low'; items: IncompleteItem[]; }

const GROUPS: Record<string, { title: string; severity: 'high' | 'medium' | 'low' }> = {
  END_NO_STATUS: { title: 'Dead end, no status', severity: 'high' },
  UNACCOUNTED_AMOUNT: { title: 'Partly unaccounted', severity: 'medium' },
  NO_INCOMING_LINK: { title: 'No incoming link', severity: 'high' },
  VICTIM_UNTRACED: { title: 'Victim debit untraced', severity: 'high' },
  HOLD_WITHOUT_TRAIL: { title: 'Hold with no trail', severity: 'medium' },
  LOW: { title: 'Estimated amount / low-confidence link', severity: 'low' },
};
const ORDER = { high: 0, medium: 1, low: 2 };

export function groupIncompleteItems(items: IncompleteItem[]): IncompleteGroup[] {
  const groups = new Map<string, IncompleteGroup>();
  for (const item of items) {
    const codes = new Set(item.reasons.map((reason) => reason.code));
    const key = codes.has('END_NO_STATUS') ? 'END_NO_STATUS' : codes.has('UNACCOUNTED_AMOUNT') ? 'UNACCOUNTED_AMOUNT' : codes.has('NO_INCOMING_LINK') ? 'NO_INCOMING_LINK' : codes.has('VICTIM_UNTRACED') ? 'VICTIM_UNTRACED' : codes.has('HOLD_WITHOUT_TRAIL') ? 'HOLD_WITHOUT_TRAIL' : 'LOW';
    const definition = GROUPS[key];
    const group = groups.get(key) ?? { key, title: definition.title, severity: definition.severity, items: [] };
    group.items.push(item);
    groups.set(key, group);
  }
  return [...groups.values()].sort((left, right) => ORDER[left.severity] - ORDER[right.severity] || left.title.localeCompare(right.title));
}

@Component({
  selector: 'app-incomplete-nodes', standalone: true, imports: [CommonModule],
  templateUrl: './incomplete-nodes.html', styleUrl: './incomplete-nodes.scss', changeDetection: ChangeDetectionStrategy.OnPush,
})
export class IncompleteNodesComponent {
  readonly sourceItems = signal<IncompleteItem[]>([]);
  readonly groups = computed(() => groupIncompleteItems(this.sourceItems()));
  readonly tooltip = signal<{ x: number; y: number; item: IncompleteItem } | null>(null);
  private selected: string | null = null;
  @Input() set selectedId(value: string | null) {
    this.selected = value;
    if (value) queueMicrotask(() => document.getElementById(this.cardIdForNode(value))?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
  }
  get selectedId(): string | null { return this.selected; }
  @Input() set items(value: IncompleteItem[] | null | undefined) { this.sourceItems.set(value ?? []); }
  @Output() readonly itemSelect = new EventEmitter<string>();

  trackGroup = (_: number, group: IncompleteGroup) => group.key;
  trackItem = (_: number, item: IncompleteItem) => item.nodeId ?? item.holdId ?? '';
  severityClass(item: IncompleteItem): string { return `severity-${item.severity}`; }
  severityText(item: IncompleteItem): string { return item.severity.toUpperCase(); }
  amount(item: IncompleteItem): string {
    const value = item.kind === 'hold' ? item.amount : item.disputedAmount ?? item.txAmount;
    return value == null ? '—' : new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' }).format(value);
  }
  cardId(item: IncompleteItem): string { return item.kind === 'node' && item.nodeId ? this.cardIdForNode(item.nodeId) : `incomplete-hold-${item.holdId}`; }
  private cardIdForNode(nodeId: string): string { return `incomplete-node-${nodeId}`; }
  accessible(item: IncompleteItem): string { return `${item.kind === 'node' ? 'Focus' : 'View'} ${item.bank || item.accountNo || 'unknown'}: ${item.reasons.map((reason) => reason.message).join(' ')}`; }
  select(item: IncompleteItem): void { if (item.kind === 'node' && item.nodeId) this.itemSelect.emit(item.nodeId); }
  keydown(event: KeyboardEvent, item: IncompleteItem): void { if ((event.key === 'Enter' || event.key === ' ') && item.kind === 'node') { event.preventDefault(); this.select(item); } }
  show(event: PointerEvent | FocusEvent, item: IncompleteItem): void {
    const point = event instanceof PointerEvent ? { x: event.clientX, y: event.clientY } : { x: 24, y: 24 };
    this.tooltip.set({ x: Math.max(8, Math.min(point.x + 12, window.innerWidth - 310)), y: Math.max(8, Math.min(point.y + 12, window.innerHeight - 180)), item });
  }
  hide(): void { this.tooltip.set(null); }
}
