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
@Injectable({
  providedIn: 'root',
})
export class Case_Details {
  private readonly http = inject(HttpClient);
  
    getCaseDetail(uploadId: string): Observable<CaseDetail> {
      return this.http.get<CaseDetail>(`${environment.apiBaseUrl}/api/cases/${uploadId}`);
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
