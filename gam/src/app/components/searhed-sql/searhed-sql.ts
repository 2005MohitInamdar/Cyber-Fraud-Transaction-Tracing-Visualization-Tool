import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, Output, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Case_Details } from '../../services/case_Details/case-details';

@Component({
  selector: 'app-searhed-sql',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './searhed-sql.html',
  styleUrl: './searhed-sql.scss',
})
export class SearhedSQL {
  @Input({ required: true }) uploadId = '';
  @Output() readonly activeTabChange = new EventEmitter<'all' | 'search'>();

  readonly activeTab = signal<'all' | 'search'>('all');
  readonly searchQuery = signal('');
  readonly isSearching = signal(false);
  readonly searchResults = signal<Record<string, unknown>[] | null>(null);
  readonly searchError = signal<string | null>(null);
  readonly generatedSql = signal<string | null>(null);

  private readonly caseDetailService = inject(Case_Details);

  selectTab(tab: 'all' | 'search'): void {
    this.activeTab.set(tab);
    this.activeTabChange.emit(tab);
  }

  search(): void {
    const query = this.searchQuery().trim();
    if (!query || this.isSearching()) return;

    this.isSearching.set(true);
    this.searchResults.set(null);
    this.searchError.set(null);
    this.generatedSql.set(null);

    this.caseDetailService.searchCase(this.uploadId, query).subscribe({
      next: (response) => {
        this.searchResults.set(response.rows);
        this.generatedSql.set(response.sql);
        this.isSearching.set(false);
        this.selectTab('search');
      },
      error: (error) => {
        const detail = error?.error?.detail;
        this.searchError.set(
          typeof detail === 'string'
            ? detail
            : detail?.message ?? 'Something went wrong while searching this case.',
        );
        this.generatedSql.set(typeof detail === 'object' ? detail?.sql ?? null : null);
        this.isSearching.set(false);
        this.selectTab('search');
      },
    });
  }

  keys(row: Record<string, unknown>): string[] {
    return Object.keys(row);
  }
}
