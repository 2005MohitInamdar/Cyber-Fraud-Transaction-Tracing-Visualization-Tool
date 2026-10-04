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
  };
  nodes: CaseGraphNode[];
  edges: CaseGraphEdge[];
}

@Injectable({
  providedIn: 'root',
})
export class Case_Details {
  private readonly http = inject(HttpClient);
  
    getCaseDetail(uploadId: string): Observable<CaseDetail> {
      return this.http.get<CaseDetail>(`${environment.apiBaseUrl}/api/cases/${uploadId}`);
    }

    getCaseGraph(uploadId: string): Observable<CaseGraph> {
      return this.http.get<CaseGraph>(`${environment.apiBaseUrl}/api/cases/${uploadId}/graph`);
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
