import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink, ActivatedRoute } from '@angular/router';
import { CaseDetail, Case_Details } from '../../services/case_Details/case-details';

@Component({
  selector: 'app-case-details',
  standalone:true,
  imports: [CommonModule, RouterLink],
  templateUrl: './case-details.html',
  styleUrl: './case-details.scss',
})
export class CaseDetails implements OnInit {
  readonly caseDetail = signal<CaseDetail | null>(null);
  readonly isLoading = signal(true);
  readonly loadError = signal(false);

  private readonly route = inject(ActivatedRoute);
  private readonly caseDetailService = inject(Case_Details);

  ngOnInit(): void {
    const uploadId = this.route.snapshot.paramMap.get('uploadId');
    if (!uploadId) {
      this.loadError.set(true);
      this.isLoading.set(false);
      return;
    }

    this.caseDetailService.getCaseDetail(uploadId).subscribe({
      next: (detail) => {
        this.caseDetail.set(detail);
        this.isLoading.set(false);
      },
      error: (error) => {
        console.error('Failed to load case detail:', error);
        this.loadError.set(true);
        this.isLoading.set(false);
      },
    });
  }

  keys(row: Record<string, unknown>): string[] {
    return Object.keys(row);
  }

  hasValues(row: Record<string, unknown>): boolean {
    return Object.keys(row).length > 0;
  }
}
