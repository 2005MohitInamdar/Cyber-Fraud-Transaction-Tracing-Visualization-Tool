import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environment'; 


export interface CaseUpload {
  fileName: string | null;
  fileSize: number | null;
  contentType: string | null;
  status: string | null;
  filePath: string | null;
  createdAt: string | null;
  completedAt: string | null;
}

export interface CaseDetail {
  uploadId: string;
  leadOfficer: {
    inspectorName: string;
    inspectorRank: string;
    inspectorBranch: string;
  };
  upload: CaseUpload;
  tables: {
    amountSummary: Record<string, unknown>[];
    complaintMeta: Record<string, unknown>;
    complaintTransactions: Record<string, unknown>[];
    failedTransactions: Record<string, unknown>[];
    holdAccounts: Record<string, unknown>[];
    lienTransactions: Record<string, unknown>[];
    noActionReferences: Record<string, unknown>[];
    pendingTransactions: Record<string, unknown>[];
  };
}

export interface CaseSearchResult {
  sql: string;
  rows: Record<string, unknown>[];
}

export interface CaseGraphHold {
  holdId: string;
  amount: number | null;
  date: string | null;
  actionTakenBy: string;
  matchRule: string;
  confidence: number | null;
}

export interface CompletenessReason {
  code: string;
  message: string;
}

export interface NodeCompleteness {
  status: 'complete' | 'incomplete';
  reasons: CompletenessReason[];
  resolvedBy: 'hold' | 'pending' | 'withdrawal_note' | null;
}

export interface IncompleteItem {
  kind: 'node' | 'hold';
  nodeId?: string;
  holdId?: string;
  layer?: number;
  bank?: string;
  accountNo: string;
  utr?: string;
  disputedAmount?: number | null;
  txAmount?: number | null;
  unaccountedAmount?: number | null;
  amount?: number | null;
  date?: string | null;
  actionTakenBy?: string;
  remarks?: string;
  severity: 'high' | 'medium' | 'low';
  reasons: CompletenessReason[];
}

export interface CaseGraphNode {
  id: string;
  layer: number;
  bank: string;
  actionTakenBy: string;
  accountNo: string;
  utr: string;
  txAmount: number | null;
  disputedAmount: number | null;
  amountEstimated: boolean;
  frozenAmount: number;
  unaccountedAmount: number | null;
  embeddedIds: unknown[];
  rootIds: unknown[];
  remarks: string;
  role: 'victim' | 'intermediate' | 'endOfTrail' | 'isolated';
  holds: CaseGraphHold[];
  completeness?: NodeCompleteness;
  addedInRevision?: number;
  fromBankResponse?: boolean;
}

export interface CaseGraphEdge {
  source: string;
  target: string;
  matchRule: string;
  confidence: number | null;
  amountPassed: number | null;
  ambiguous: boolean;
  merged: boolean;
  amountEstimated: boolean;
}

export interface CaseGraph {
  hasGraph: boolean;
  revision?: number;
  revisions?: { revision: number; createdAt: string | null; changeNote: string | null }[];
  summary: {
    ackNo: string | null;
    status: string | null;
    baseDebitTotal: number | null;
    reportedFraudTotal: number | null;
    holdTotal: number | null;
    reportedLienTotal: number | null;
    holdsMatchLien: boolean;
    nodeCount: number;
    edgeCount: number;
    layers: number[];
    unmatchedHoldCount: number;
    incompleteCount?: number;
    orphanHoldCount?: number;
  };
  nodes: CaseGraphNode[];
  edges: CaseGraphEdge[];
  incomplete?: IncompleteItem[];
  orphanHolds?: IncompleteItem[];
}

export interface BankResponseParseResult {
  responseId: number;
  format: 'ncrp_table' | 'unknown';
  confidence: 'high' | 'medium' | 'low';
  warnings: string[];
  lienRows?: Record<string, unknown>[];
  holdRows?: Record<string, unknown>[];
  rows?: BankResponseRow[];
  anchorCandidates?: { nodeId: string; bank: string; layer: number; incomplete: boolean; score: number }[];
  suggestedAnchorNodeId?: string | null;
  suggestedLayerShift?: number;
}

export interface BankResponseRow {
  id: number;
  table: 'lien_transactions' | 'hold_accounts';
  data: Record<string, unknown>;
  isDuplicate: boolean;
  duplicateReason?: string | null;
  included: boolean;
}

export interface BankResponseApplyResult {
  revision: number;
  newNodeCount: number;
  resolvedNodeCount: number;
  newNodeIds: string[];
  resolvedNodeIds: string[];
  warnings: string[];
}

@Injectable({
  providedIn: 'root',
})
export class Case_Details {
  private readonly http = inject(HttpClient);
  
    getCaseDetail(uploadId: string): Observable<CaseDetail> {
      return this.http.get<CaseDetail>(`${environment.apiBaseUrl}/api/cases/${uploadId}`);
    }

    getCaseGraph(uploadId: string, revision?: number): Observable<CaseGraph> {
      const suffix = revision == null ? '' : `?revision=${encodeURIComponent(revision)}`;
      return this.http.get<CaseGraph>(`${environment.apiBaseUrl}/api/cases/${uploadId}/graph${suffix}`);
    }

    parseBankResponse(uploadId: string, file: File, requisitionId?: number): Observable<BankResponseParseResult> {
      const form = new FormData();
      form.append('file', file);
      if (requisitionId != null) form.append('requisitionId', String(requisitionId));
      return this.http.post<BankResponseParseResult>(`${environment.apiBaseUrl}/api/cases/${uploadId}/responses/parse`, form);
    }

    updateResponseRow(uploadId: string, responseId: number, rowId: number, data: Record<string, unknown>, included?: boolean): Observable<unknown> {
      return this.http.patch(`${environment.apiBaseUrl}/api/cases/${uploadId}/responses/${responseId}/rows/${rowId}`, { data, included });
    }

    listBankResponses(uploadId: string): Observable<{ responses: unknown[] }> {
      return this.http.get<{ responses: unknown[] }>(`${environment.apiBaseUrl}/api/cases/${uploadId}/responses`);
    }

    getUploadProgress(uploadId: string): Observable<{ events: { message: string; state: string; at: string }[] }> {
      return this.http.get<{ events: { message: string; state: string; at: string }[] }>(`${environment.apiBaseUrl}/api/uploads/${uploadId}/progress`);
    }

    listRequisitions(uploadId: string): Observable<{ history: { id: number; bankName: string; status: string; sentAt: string | null }[] }> {
      return this.http.get<{ history: { id: number; bankName: string; status: string; sentAt: string | null }[] }>(`${environment.apiBaseUrl}/api/cases/${uploadId}/requisitions`);
    }

    addResponseRow(uploadId: string, responseId: number, tableName: 'lien_transactions' | 'hold_accounts', data: Record<string, unknown>): Observable<BankResponseRow> {
      return this.http.post<BankResponseRow>(`${environment.apiBaseUrl}/api/cases/${uploadId}/responses/${responseId}/rows`, { tableName, data });
    }

    applyBankResponse(uploadId: string, responseId: number, body: { anchorNodeId?: string; layerShift?: number; annotation?: { nodeId: string; kind: string; note?: string }; changeNote?: string }): Observable<BankResponseApplyResult> {
      return this.http.post<BankResponseApplyResult>(`${environment.apiBaseUrl}/api/cases/${uploadId}/responses/${responseId}/apply`, body);
    }

    sendReportEmail(uploadId: string): Observable<{ message: string; subject: string }> {
      return this.http.post<{ message: string; subject: string }>(
        `${environment.apiBaseUrl}/api/cases/${uploadId}/report-email`,
        {},
      );
    }

    searchCase(uploadId: string, query: string): Observable<CaseSearchResult> {
      return this.http.post<CaseSearchResult>(
        `${environment.apiBaseUrl}/api/cases/${uploadId}/search`,
        { query },
      );
    }
}
