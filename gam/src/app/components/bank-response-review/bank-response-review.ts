import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { BankResponseParseResult, BankResponseRow, CaseGraph, Case_Details } from '../../services/case_Details/case-details';

@Component({
  selector: 'app-bank-response-review',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './bank-response-review.html',
  styleUrls: ['./bank-response-review.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BankResponseReview {
  @Input({ required: true }) uploadId = '';
  @Input({ required: true }) graph: CaseGraph | null = null;
  @Output() applied = new EventEmitter<void>();
  private readonly cases = inject(Case_Details);
  readonly open = signal(false);
  readonly stage = signal<1 | 2 | 3 | 4>(1);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly result = signal<BankResponseParseResult | null>(null);
  readonly rows = signal<BankResponseRow[]>([]);
  readonly anchor = signal('');
  readonly shift = signal(0);
  readonly annotation = signal(false);
  readonly annotationNode = signal('');
  readonly annotationNote = signal('');
  readonly requisitionId = signal('');
  readonly requisitions = signal<{ id: number; bankName: string; status: string; sentAt: string | null }[]>([]);
  readonly progressMessage = signal('Preparing rebuild…');
  private progressTimer?: number;
  readonly responseId = computed(() => this.result()?.responseId ?? null);
  readonly selectedCount = computed(() => this.rows().filter(row => row.included && !row.isDuplicate).length);
  readonly otherAnchorNodes = computed(() => {
    const used = new Set((this.result()?.anchorCandidates ?? []).map(item => item.nodeId));
    return (this.graph?.nodes ?? []).filter(node => !used.has(node.id));
  });
  readonly preview = computed(() => {
    const layers = this.rows().filter((row) => row.table === 'lien_transactions' && row.included && !row.isDuplicate)
      .map((row) => Number(row.data['layer'] ?? 0)).filter((value) => Number.isFinite(value));
    if (!layers.length) return 'No lien rows selected.';
    return `Response layers ${Math.min(...layers)}–${Math.max(...layers)} become ${Math.min(...layers) + this.shift()}–${Math.max(...layers) + this.shift()}.`;
  });

  show(): void {
    this.open.set(true); this.stage.set(1); this.error.set(null);
    this.cases.listRequisitions(this.uploadId).subscribe({ next: result => this.requisitions.set(result.history.filter(item => item.status === 'sent')), error: () => this.requisitions.set([]) });
  }
  close(): void { if (!this.busy()) this.open.set(false); }
  chooseFile(event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (!file) return;
    if (file.size > 10 * 1024 * 1024 || file.type !== 'application/pdf') { this.error.set('Choose a PDF up to 10 MB.'); return; }
    this.busy.set(true); this.error.set(null);
    const requestId = Number(this.requisitionId());
    this.cases.parseBankResponse(this.uploadId, file, Number.isInteger(requestId) && requestId > 0 ? requestId : undefined).subscribe({
      next: result => { this.result.set(result); this.rows.set(result.rows ?? []); this.anchor.set(result.suggestedAnchorNodeId ?? ''); this.shift.set(result.suggestedLayerShift ?? 0); this.stage.set(2); this.busy.set(false); },
      error: error => { this.error.set(error?.error?.detail ?? 'Could not parse this response PDF.'); this.busy.set(false); },
    });
  }
  toggle(row: BankResponseRow): void {
    if (row.isDuplicate || this.responseId() == null) return;
    const included = !row.included;
    this.rows.update(rows => rows.map(item => item.id === row.id ? { ...item, included } : item));
    this.cases.updateResponseRow(this.uploadId, this.responseId()!, row.id, {}, included).subscribe({ error: () => this.error.set('Could not save this row choice.') });
  }
  setAnchor(event: Event): void {
    const id = (event.target as HTMLSelectElement).value;
    this.anchor.set(id);
    const node = this.graph?.nodes.find(item => item.id === id);
    const min = Math.min(...this.rows().filter(row => row.table === 'lien_transactions' && row.included && !row.isDuplicate).map(row => Number(row.data['layer'] ?? 1)));
    if (node && Number.isFinite(min)) this.shift.set(node.layer + 1 - min);
  }
  addRow(tableName: BankResponseRow['table'] = 'lien_transactions'): void {
    if (this.responseId() == null) return;
    const data = tableName === 'lien_transactions'
      ? { layer: 1, bank_fi: '', account_no: '', transaction_id: '', transaction_amount: '', disputed_amount: '' }
      : { account_no: '', hold_date: '', hold_amount: '', action_taken_by: '' };
    this.cases.addResponseRow(this.uploadId, this.responseId()!, tableName, data).subscribe({
      next: row => this.rows.update(rows => [...rows, row]), error: () => this.error.set('Could not add a manual row.'),
    });
  }
  fields(row: BankResponseRow): string[] {
    return row.table === 'lien_transactions'
      ? ['layer', 'bank_fi', 'account_no', 'transaction_id', 'transaction_amount', 'disputed_amount']
      : ['account_no', 'hold_date', 'hold_amount', 'action_taken_by'];
  }
  rowsFor(table: BankResponseRow['table']): BankResponseRow[] { return this.rows().filter(row => row.table === table); }
  editField(row: BankResponseRow, field: string, event: Event): void {
    if (row.isDuplicate || this.responseId() == null) return;
    const value = (event.target as HTMLInputElement).value;
    const data = { ...row.data, [field]: value };
    this.rows.update(rows => rows.map(item => item.id === row.id ? { ...item, data } : item));
    this.cases.updateResponseRow(this.uploadId, this.responseId()!, row.id, { [field]: value }).subscribe({ error: () => this.error.set('Could not save this row edit.') });
  }
  setAnnotationNode(event: Event): void { this.annotationNode.set((event.target as HTMLSelectElement).value); }
  apply(): void {
    if (this.responseId() == null) return;
    this.busy.set(true); this.error.set(null); this.progressMessage.set('Applying reviewed response…');
    const startedAt = Date.now();
    this.progressTimer = window.setInterval(() => this.cases.getUploadProgress(this.uploadId).subscribe({
      next: result => {
        const latest = result.events.filter(item => Date.parse(item.at) >= startedAt).at(-1);
        if (latest) this.progressMessage.set(latest.message);
      },
    }), 700);
    const annotation = this.annotation() && this.annotationNode() ? { nodeId: this.annotationNode(), kind: 'no_onward', note: this.annotationNote() } : undefined;
    this.cases.applyBankResponse(this.uploadId, this.responseId()!, { anchorNodeId: this.anchor() || undefined, layerShift: this.shift(), annotation }).subscribe({
      next: () => { this.stopProgress(); this.busy.set(false); this.stage.set(4); this.applied.emit(); },
      error: error => { this.stopProgress(); this.busy.set(false); this.error.set(error?.error?.detail ?? 'Could not rebuild the case.'); },
    });
  }
  reviewComplete(): void { this.stage.set(3); }
  stopProgress(): void { if (this.progressTimer !== undefined) { window.clearInterval(this.progressTimer); this.progressTimer = undefined; } }
}
