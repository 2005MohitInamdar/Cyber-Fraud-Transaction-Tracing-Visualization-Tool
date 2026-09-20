// import { Component, OnInit, OnDestroy } from '@angular/core';
// import { CommonModule } from '@angular/common';
// import { FormsModule } from '@angular/forms';
// import { HttpClient } from '@angular/common/http';
// import { Subscription, firstValueFrom } from 'rxjs';
// import { NewCaseData } from '../../services/new-case-data';
// import { environment } from '../../environment';

// // ─── Interfaces ──────────────────────────────────────────────────────────────

// /** Metadata for a single 5 MB chunk */
// export interface ChunkMetadata {
//   chunkNumber: number; // 1-indexed
//   size: number;        // bytes in this chunk
//   hash: string;        // SHA-256 hex of the chunk bytes
// }

// /** Top-level file upload metadata sent to the backend to initiate an upload */
// export interface FileUploadMetadata {
//   uploadId: string;    // UUID generated client-side
//   fileName: string;
//   fileSize: number;    // total bytes
//   contentType: string; // MIME type
//   chunkSize: number;   // standard chunk size in bytes (5 MB)
//   totalChunks: number;
//   fileHash: string;    // SHA-256 hex of the entire file
//   status: 'pending' | 'uploading' | 'complete' | 'failed';
//   chunks: ChunkMetadata[];
// }

// interface UploadProgressEvent {
//   message: string;
//   state: 'processing' | 'complete' | 'failed';
//   at: string;
// }

// // ─── Component ───────────────────────────────────────────────────────────────

// @Component({
//   selector: 'app-new-fraud-case',
//   standalone: true,
//   imports: [CommonModule, FormsModule],
//   templateUrl: './new-fraud-case.html',
//   styleUrl: './new-fraud-case.scss',
// })
// export class NewFraudCase implements OnInit, OnDestroy {

//   // Modal state comes from the shared service
//   isOpen: boolean = false;
//   private modalSub!: Subscription;

//   // Inspector form fields
//   inspectorName: string = '';
//   inspectorBranch: string = ''
//   inspectorRank: string = '';

//   // File upload state
//   selectedFile: File | null = null;
//   isDragging: boolean = false;
//   isProcessing: boolean = false;  // true while hashing / sending
//   progressMessage = '';
//   progressEvents: UploadProgressEvent[] = [];
//   private progressPollId?: number;
//   private receivedCompletionKeyword = false;

//   /** Standard chunk size: 5 MB */
//   private readonly CHUNK_SIZE = 5 * 1024 * 1024;

//   constructor(
//     private newCaseData: NewCaseData,
//     private http: HttpClient,
//   ) {}

//   ngOnInit(): void {
//     this.modalSub = this.newCaseData.isModalOpen$.subscribe(
//       (state) => (this.isOpen = state)
//     );
//   }

//   ngOnDestroy(): void {
//     this.modalSub.unsubscribe();
//     this.stopProgressPolling();
//   }

//   // ─── Modal Control ───────────────────────────────────────────────────────

//   close(): void {
//     this.newCaseData.closeModal();
//   }

//   onOverlayClick(event: MouseEvent): void {
//     if ((event.target as HTMLElement).classList.contains('modal-overlay')) {
//       this.close();
//     }
//   }

//   // ─── Drag & Drop / File Selection ────────────────────────────────────────

//   onDragOver(event: DragEvent): void {
//     event.preventDefault();
//     this.isDragging = true;
//   }

//   onDragLeave(): void {
//     this.isDragging = false;
//   }

//   onDrop(event: DragEvent): void {
//     event.preventDefault();
//     this.isDragging = false;
//     const file = event.dataTransfer?.files[0];
//     if (file) this.handleFile(file);
//   }

//   onFileSelected(event: Event): void {
//     const input = event.target as HTMLInputElement;
//     if (input.files && input.files[0]) {
//       this.handleFile(input.files[0]);
//     }
//   }

//   handleFile(file: File): void {
//     const allowedTypes = [
//       'application/pdf',
//       'application/vnd.ms-excel',
//       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
//     ];
//     if (!allowedTypes.includes(file.type)) {
//       alert('Only PDF and Excel files are accepted.');
//       return;
//     }
//     this.selectedFile = file;
//     console.log('File selected:', file.name, `(${this.formatFileSize(file.size)})`);
//   }

//   removeFile(): void {
//     this.selectedFile = null;
//   }

//   getFileIcon(): string {
//     if (!this.selectedFile) return '';
//     return this.selectedFile.type === 'application/pdf' ? '📄' : '📊';
//   }

//   formatFileSize(bytes: number): string {
//     if (bytes < 1024) return bytes + ' B';
//     if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
//     return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
//   }

//   // ─── Crypto Helpers ──────────────────────────────────────────────────────

//   /** Compute SHA-256 of an ArrayBuffer → hex string */
//   private async sha256Hex(buffer: ArrayBuffer): Promise<string> {
//     const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
//     return Array.from(new Uint8Array(hashBuffer))
//       .map((b) => b.toString(16).padStart(2, '0'))
//       .join('');
//   }

//   /** Read a Blob as an ArrayBuffer */
//   private readBlobAsArrayBuffer(blob: Blob): Promise<ArrayBuffer> {
//     return new Promise((resolve, reject) => {
//       const reader = new FileReader();
//       reader.onload  = () => resolve(reader.result as ArrayBuffer);
//       reader.onerror = () => reject(reader.error);
//       reader.readAsArrayBuffer(blob);
//     });
//   }

//   // ─── Metadata Builders ───────────────────────────────────────────────────

//   /**
//    * Slices the file into 5 MB chunks, hashes each chunk individually,
//    * then hashes the full file for the top-level fileHash field.
//    * Returns the complete FileUploadMetadata ready to POST.
//    */
//   private async buildFileUploadMetadata(file: File): Promise<FileUploadMetadata> {
//     const totalChunks = Math.ceil(file.size / this.CHUNK_SIZE);

//     // 1. Hash the entire file
//     const fullBuffer = await this.readBlobAsArrayBuffer(file);
//     const fileHash   = await this.sha256Hex(fullBuffer);

//     // 2. Hash each chunk independently and build ChunkMetadata[]
//     const chunks: ChunkMetadata[] = [];
//     for (let i = 0; i < totalChunks; i++) {
//       const start     = i * this.CHUNK_SIZE;
//       const end       = Math.min(start + this.CHUNK_SIZE, file.size);
//       const chunkBlob = file.slice(start, end);

//       const chunkBuffer = await this.readBlobAsArrayBuffer(chunkBlob);
//       const chunkHash   = await this.sha256Hex(chunkBuffer);

//       const chunkMeta: ChunkMetadata = {
//         chunkNumber: i + 1,       // 1-indexed
//         size: chunkBlob.size,
//         hash: chunkHash,
//       };

//       console.log(`  Chunk ${i + 1}/${totalChunks}  size=${this.formatFileSize(chunkMeta.size)}  hash=${chunkHash.slice(0, 12)}…`);
//       chunks.push(chunkMeta);
//     }

//     // 3. Assemble top-level metadata
//     return {
//       uploadId:    crypto.randomUUID(),
//       fileName:    file.name,
//       fileSize:    file.size,
//       contentType: file.type,
//       chunkSize:   this.CHUNK_SIZE,
//       totalChunks,
//       fileHash,
//       status:      'pending',
//       chunks,
//     };
//   }

//   // ─── Create Case (called by the button) ──────────────────────────────────

//   async createCase(): Promise<void> {
//     if (!this.selectedFile) {
//       alert('Please attach a case file before submitting.');
//       return;
//     }

//     this.isProcessing = true;
//     this.progressMessage = 'Preparing your case file…';
//     this.progressEvents = [];
//     this.receivedCompletionKeyword = false;
//     console.group('📦 Creating fraud case upload session');

//     let metadata: FileUploadMetadata;
//     try {
//       console.log('Computing file metadata & chunk hashes…');
//       metadata = await this.buildFileUploadMetadata(this.selectedFile);
//       this.progressMessage = 'Case file prepared. Creating the upload session…';

//       console.log('File upload metadata:', {
//         uploadId:    metadata.uploadId,
//         fileName:    metadata.fileName,
//         fileSize:    this.formatFileSize(metadata.fileSize),
//         contentType: metadata.contentType,
//         chunkSize:   this.formatFileSize(metadata.chunkSize),
//         totalChunks: metadata.totalChunks,
//         fileHash:    metadata.fileHash,
//         status:      metadata.status,
//         chunks:      metadata.chunks,
//       });
//     } catch (err) {
//       console.error('Failed to compute file metadata:', err);
//       alert('Error processing file. Please try again.');
//       this.isProcessing = false;
//       console.groupEnd();
//       return;
//     }

//     // POST the metadata to the backend to initiate the upload session
//     try {
//       console.log('Sending POST /api/uploads/initiate…');
//       const payload = {
//         leadOfficer: {
//           inspectorName: this.inspectorName,
//           inspectorRank: this.inspectorRank,
//           inspectorBranch: this.inspectorBranch,
//         },
//         fileMetadata: metadata,
//       };
//       const result = await firstValueFrom(
//         this.http.post<{ message: string }>(`${environment.apiBaseUrl}/api/uploads/initiate`, payload)
//       );

//       console.log('✅', result.message);
//       this.startProgressPolling(metadata.uploadId);
//       this.progressMessage = 'Uploading the case file securely…';

//       // ── Chunk-send simulation (no API yet) ──────────────────────────
//       await this.sendChunks(this.selectedFile!, metadata);

//     } catch (err) {
//       console.error('Failed to initiate upload:', err);
//       alert('Could not create upload session. Please try again.');
//     } finally {
//       this.isProcessing = false;
//       this.stopProgressPolling();
//       console.groupEnd();
//     }
//   }

//   /** Max attempts per chunk (1 initial + 2 retries) */
//   private readonly MAX_CHUNK_ATTEMPTS = 3;

//   /** Base delay in ms for exponential backoff (1s → 2s → 4s) */
//   private readonly RETRY_BASE_DELAY_MS = 1000;

//   // ─── Chunk upload with retry ─────────────────────────────────────────────

//   /**
//    * Sends a single chunk to the backend with exponential backoff retry.
//    * Attempts: 1st try → wait 1s → 2nd try → wait 2s → 3rd try → throw.
//    */
//   private async sendOneChunk(
//     form: FormData,
//     chunkMeta: ChunkMetadata,
//     totalChunks: number,
//   ): Promise<{
//     chunkNumber: number;
//     hashVerified: boolean;
//     totalChunks: number;
//     chunksReceivedSoFar: number;
//     allChunksReceived: boolean;
//     completionKeyword?: string | null;
//   }> {

//     let lastError: unknown;

//     for (let attempt = 1; attempt <= this.MAX_CHUNK_ATTEMPTS; attempt++) {
//       try {
//         if (attempt > 1) {
//           const delay = this.RETRY_BASE_DELAY_MS * Math.pow(2, attempt - 2); // 1s, 2s, 4s…
//           console.warn(
//             `%c⏳ Chunk ${chunkMeta.chunkNumber}/${totalChunks} — attempt ${attempt}/${this.MAX_CHUNK_ATTEMPTS} (waiting ${delay}ms…)`,
//             'color: #fb923c; font-weight: bold;'
//           );
//           await new Promise(r => setTimeout(r, delay));
//         }

//         const result = await firstValueFrom(
//           this.http.post<{
//             uploadId: string;
//             chunkNumber: number;
//             size: number;
//             hash: string;
//             hashVerified: boolean;
//             totalChunks: number;
//             chunksReceivedSoFar: number;
//             allChunksReceived: boolean;
//             completionKeyword?: string | null;
//           }>(`${environment.apiBaseUrl}/api/uploads/chunk`, form, {
//             withCredentials: true,
//           })
//         );

//         return result;   // success — exit retry loop

//       } catch (err) {
//         lastError = err;
//         console.error(
//           `%c❌ Chunk ${chunkMeta.chunkNumber}/${totalChunks} — attempt ${attempt}/${this.MAX_CHUNK_ATTEMPTS} failed:`,
//           'color: #f87171; font-weight: bold;',
//           err
//         );
//       }
//     }

//     // All attempts exhausted
//     throw new Error(
//       `Chunk ${chunkMeta.chunkNumber} failed after ${this.MAX_CHUNK_ATTEMPTS} attempts.`
//     );
//   }

//   /**
//    * Iterates over every chunk, builds FormData, and sends with retry.
//    * Logs detailed metadata + Blob before each send.
//    * Throws at the end if any chunks permanently failed.
//    */
//   private async sendChunks(file: File, metadata: FileUploadMetadata): Promise<void> {
//     console.group(`🚀 Uploading ${metadata.totalChunks} chunk(s) for uploadId=${metadata.uploadId}`);

//     const failedChunks: number[] = [];

//     for (const chunkMeta of metadata.chunks) {
//       const start     = (chunkMeta.chunkNumber - 1) * this.CHUNK_SIZE;
//       const end       = start + chunkMeta.size;
//       const chunkBlob = file.slice(start, end, file.type);

//       // ── Log chunk metadata + Blob before sending ──────────────────────────
//       console.group(
//         `%c📤 Chunk ${chunkMeta.chunkNumber}/${metadata.totalChunks} — sending…`,
//         'color: #facc15; font-weight: bold;'
//       );
//       console.log('Metadata :', {
//         chunkNumber : chunkMeta.chunkNumber,
//         byteRange   : `${start} – ${end - 1}`,
//         size        : this.formatFileSize(chunkMeta.size),
//         sizeBytes   : chunkMeta.size,
//         hash        : chunkMeta.hash,
//         uploadId    : metadata.uploadId,
//       });
//       console.log('Blob     :', chunkBlob);

//       // ── Build multipart/form-data payload ─────────────────────────────────
//       const form = new FormData();
//       form.append('uploadId',    metadata.uploadId);
//       form.append('chunkNumber', String(chunkMeta.chunkNumber));
//       form.append('chunk',       chunkBlob, `chunk_${chunkMeta.chunkNumber}`);

//       // ── Send with retry ───────────────────────────────────────────────────
//       try {
//         const result = await this.sendOneChunk(form, chunkMeta, metadata.totalChunks);
//         console.log(
//           `%c✅ Chunk ${result.chunkNumber}/${result.totalChunks} accepted` +
//           ` — hashVerified=${result.hashVerified}` +
//           ` — progress: ${result.chunksReceivedSoFar}/${result.totalChunks}`,
//           'color: #4ade80; font-weight: bold;'
//         );
//         if (result.allChunksReceived) {
//           this.progressMessage = 'Upload complete. Starting case-file analysis…';
//           console.log(
//             '%c🎉 Backend confirmed: all chunks received!',
//             'color: #a78bfa; font-size: 14px; font-weight: bold;'
//           );
//         }
//         if (result.completionKeyword === 'CASE_PROCESSING_COMPLETE') {
//           this.completeCaseProcessing();
//         }
//       } catch (err) {
//         // All retries exhausted for this chunk
//         console.error(
//           `%c💀 Chunk ${chunkMeta.chunkNumber} permanently failed — will report at end.`,
//           'color: #f87171; font-weight: bold;'
//         );
//         failedChunks.push(chunkMeta.chunkNumber);
//       }

//       console.groupEnd();
//       await new Promise(r => setTimeout(r, 50));  // small gap keeps logs readable
//     }

//     // ── Final report ──────────────────────────────────────────────────────────
//     if (failedChunks.length === 0) {
//       console.log(`%c🎉 All ${metadata.totalChunks} chunk(s) sent successfully!`, 'color: #4ade80; font-weight: bold;');
//       if (!this.receivedCompletionKeyword) {
//         throw new Error('The server did not confirm that case processing completed.');
//       }
//     } else {
//       const msg = `Upload incomplete — ${failedChunks.length} chunk(s) failed permanently: [${failedChunks.join(', ')}]`;
//       console.error(`%c❌ ${msg}`, 'color: #f87171; font-weight: bold;');
//       console.groupEnd();
//       throw new Error(msg);
//     }

//     console.groupEnd();
//   }

//   // private startProgressPolling(uploadId: string): void {
//   //   this.stopProgressPolling();
//   //   const refresh = () => {
//   //     this.http
//   //       .get<{ events: UploadProgressEvent[] }>(
//   //         `${environment.apiBaseUrl}/api/uploads/${uploadId}/progress`,
//   //       )
//   //       .subscribe({
//   //         next: ({ events }) => {
//   //           this.progressEvents = events;
//   //           const latest = events.at(-1);
//   //           if (latest) {
//   //             this.progressMessage = latest.message;
//   //             if (latest.state === 'complete' || latest.state === 'failed') {
//   //               this.stopProgressPolling();
//   //             }
//   //           }
//   //         },
//   //         error: (error) => console.warn('Could not refresh upload progress:', error),
//   //       });
//   //   };
//   //   refresh();
//   //   this.progressPollId = window.setInterval(refresh, 700);
//   // }
//   private startProgressPolling(uploadId: string): void {
//     this.stopProgressPolling();
//     const refresh = () => {
//       this.http
//         .get<{ events: UploadProgressEvent[] }>(
//           `${environment.apiBaseUrl}/api/uploads/${uploadId}/progress`,
//         )
//         .subscribe({
//           next: ({ events }) => {
//             this.progressEvents = events;
//             const latest = events.at(-1);
//             if (!latest) return;

//             this.progressMessage = latest.message;

//             if (latest.state === 'complete') {
//               this.stopProgressPolling();
//               this.completeCaseProcessing();
//             } else if (latest.state === 'failed') {
//               this.stopProgressPolling();
//               this.isProcessing = false;
//               alert(`Case processing failed: ${latest.message}`);
//             }
//           },
//           error: (error) => console.warn('Could not refresh upload progress:', error),
//         });
//     };
//     refresh();
//     this.progressPollId = window.setInterval(refresh, 700);
//   }

//   private stopProgressPolling(): void {
//     if (this.progressPollId !== undefined) {
//       window.clearInterval(this.progressPollId);
//       this.progressPollId = undefined;
//     }
//   }

//   /** Close the processing panel only after the backend sends its completion keyword. */
//   private completeCaseProcessing(): void {
//     this.receivedCompletionKeyword = true;
//     this.progressMessage = 'Case processing completed successfully.';
//     this.isProcessing = false;
//     this.isOpen = false;
//     this.stopProgressPolling();
//     this.selectedFile = null;
//     this.inspectorName = '';
//     this.inspectorRank = '';
//     this.inspectorBranch = '';
//     this.newCaseData.notifyCaseCreated();
//     this.newCaseData.closeModal();
//   }
// }


import { Component, OnDestroy, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { NewCaseData } from '../../services/new-case-data';
import { environment } from '../../environment';

// ─── Interfaces ──────────────────────────────────────────────────────────────

/** Metadata for a single 5 MB chunk */
export interface ChunkMetadata {
  chunkNumber: number; // 1-indexed
  size: number;        // bytes in this chunk
  hash: string;        // SHA-256 hex of the chunk bytes
}

/** Top-level file upload metadata sent to the backend to initiate an upload */
export interface FileUploadMetadata {
  uploadId: string;    // UUID generated client-side
  fileName: string;
  fileSize: number;    // total bytes
  contentType: string; // MIME type
  chunkSize: number;   // standard chunk size in bytes (5 MB)
  totalChunks: number;
  fileHash: string;    // SHA-256 hex of the entire file
  status: 'pending' | 'uploading' | 'complete' | 'failed';
  chunks: ChunkMetadata[];
}

interface UploadProgressEvent {
  message: string;
  state: 'processing' | 'complete' | 'failed';
  at: string;
}

// ─── Component ───────────────────────────────────────────────────────────────

@Component({
  selector: 'app-new-fraud-case',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './new-fraud-case.html',
  styleUrl: './new-fraud-case.scss',
})
export class NewFraudCase implements OnDestroy {

  private readonly newCaseData = inject(NewCaseData);
  private readonly http = inject(HttpClient);

  // Modal state comes from the shared service
  readonly isOpen = this.newCaseData.isModalOpen;

  // Inspector form fields
  readonly inspectorName = signal('');
  readonly inspectorBranch = signal('');
  readonly inspectorRank = signal('');

  // File upload state
  readonly selectedFile = signal<File | null>(null);
  readonly isDragging = signal(false);
  readonly isProcessing = signal(false);
  readonly progressMessage = signal('');
  readonly progressEvents = signal<UploadProgressEvent[]>([]);
  private progressPollId?: number;

  /** Standard chunk size: 5 MB */
  private readonly CHUNK_SIZE = 5 * 1024 * 1024;

  ngOnDestroy(): void {
    this.stopProgressPolling();
  }

  // ─── Modal Control ───────────────────────────────────────────────────────

  close(): void {
    this.newCaseData.closeModal();
  }

  onOverlayClick(event: MouseEvent): void {
    if (!this.isProcessing() && (event.target as HTMLElement).classList.contains('modal-overlay')) {
      this.close();
    }
  }

  // ─── Drag & Drop / File Selection ────────────────────────────────────────

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    this.isDragging.set(true);
  }

  onDragLeave(): void {
    this.isDragging.set(false);
  }

  onDrop(event: DragEvent): void {
    event.preventDefault();
    this.isDragging.set(false);
    const file = event.dataTransfer?.files[0];
    if (file) this.handleFile(file);
  }

  onFileSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (input.files && input.files[0]) {
      this.handleFile(input.files[0]);
    }
  }

  handleFile(file: File): void {
    const allowedTypes = [
      'application/pdf',
      'application/vnd.ms-excel',
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    ];
    if (!allowedTypes.includes(file.type)) {
      alert('Only PDF and Excel files are accepted.');
      return;
    }
    this.selectedFile.set(file);
    console.log('File selected:', file.name, `(${this.formatFileSize(file.size)})`);
  }

  removeFile(): void {
    this.selectedFile.set(null);
  }

  getFileIcon(): string {
    const file = this.selectedFile();
    if (!file) return '';
    return file.type === 'application/pdf' ? '📄' : '📊';
  }

  formatFileSize(bytes: number): string {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  // ─── Crypto Helpers ──────────────────────────────────────────────────────

  /** Compute SHA-256 of an ArrayBuffer → hex string */
  private async sha256Hex(buffer: ArrayBuffer): Promise<string> {
    const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
    return Array.from(new Uint8Array(hashBuffer))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  }

  /** Read a Blob as an ArrayBuffer */
  private readBlobAsArrayBuffer(blob: Blob): Promise<ArrayBuffer> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload  = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(blob);
    });
  }

  // ─── Metadata Builders ───────────────────────────────────────────────────

  /**
   * Slices the file into 5 MB chunks, hashes each chunk individually,
   * then hashes the full file for the top-level fileHash field.
   * Returns the complete FileUploadMetadata ready to POST.
   */
  private async buildFileUploadMetadata(file: File): Promise<FileUploadMetadata> {
    const totalChunks = Math.ceil(file.size / this.CHUNK_SIZE);

    // 1. Hash the entire file
    const fullBuffer = await this.readBlobAsArrayBuffer(file);
    const fileHash   = await this.sha256Hex(fullBuffer);

    // 2. Hash each chunk independently and build ChunkMetadata[]
    const chunks: ChunkMetadata[] = [];
    for (let i = 0; i < totalChunks; i++) {
      const start     = i * this.CHUNK_SIZE;
      const end       = Math.min(start + this.CHUNK_SIZE, file.size);
      const chunkBlob = file.slice(start, end);

      const chunkBuffer = await this.readBlobAsArrayBuffer(chunkBlob);
      const chunkHash   = await this.sha256Hex(chunkBuffer);

      const chunkMeta: ChunkMetadata = {
        chunkNumber: i + 1,       // 1-indexed
        size: chunkBlob.size,
        hash: chunkHash,
      };

      console.log(`  Chunk ${i + 1}/${totalChunks}  size=${this.formatFileSize(chunkMeta.size)}  hash=${chunkHash.slice(0, 12)}…`);
      chunks.push(chunkMeta);
    }

    // 3. Assemble top-level metadata
    return {
      uploadId:    crypto.randomUUID(),
      fileName:    file.name,
      fileSize:    file.size,
      contentType: file.type,
      chunkSize:   this.CHUNK_SIZE,
      totalChunks,
      fileHash,
      status:      'pending',
      chunks,
    };
  }

  // ─── Create Case (called by the button) ──────────────────────────────────

  async createCase(): Promise<void> {
    const selectedFile = this.selectedFile();
    if (!selectedFile) {
      alert('Please attach a case file before submitting.');
      return;
    }

    this.isProcessing.set(true);
    this.progressMessage.set('Preparing your case file…');
    this.progressEvents.set([]);
    console.group('📦 Creating fraud case upload session');

    let metadata: FileUploadMetadata;
    try {
      console.log('Computing file metadata & chunk hashes…');
      metadata = await this.buildFileUploadMetadata(selectedFile);
      this.progressMessage.set('Case file prepared. Creating the upload session…');

      console.log('File upload metadata:', {
        uploadId:    metadata.uploadId,
        fileName:    metadata.fileName,
        fileSize:    this.formatFileSize(metadata.fileSize),
        contentType: metadata.contentType,
        chunkSize:   this.formatFileSize(metadata.chunkSize),
        totalChunks: metadata.totalChunks,
        fileHash:    metadata.fileHash,
        status:      metadata.status,
        chunks:      metadata.chunks,
      });
    } catch (err) {
      console.error('Failed to compute file metadata:', err);
      alert('Error processing file. Please try again.');
      this.isProcessing.set(false);
      console.groupEnd();
      return;
    }

    // POST the metadata to the backend to initiate the upload session
    try {
      console.log('Sending POST /api/uploads/initiate…');
      const payload = {
        leadOfficer: {
          inspectorName: this.inspectorName(),
          inspectorRank: this.inspectorRank(),
          inspectorBranch: this.inspectorBranch(),
        },
        fileMetadata: metadata,
      };
      const result = await firstValueFrom(
        this.http.post<{ message: string }>(`${environment.apiBaseUrl}/api/uploads/initiate`, payload)
      );

      console.log('✅', result.message);
      this.startProgressPolling(metadata.uploadId);
      this.progressMessage.set('Uploading the case file securely…');

      await this.sendChunks(selectedFile, metadata);

      // All chunks sent — DO NOT close the modal or stop polling here.
      // Polling is the source of truth for when backend analysis finishes.
      this.progressMessage.set('Upload complete. Awaiting case analysis…');

    } catch (err) {
      // Genuine failure: initiate call failed, or a chunk permanently failed.
      console.error('Failed to complete upload:', err);
      alert('Could not complete the upload. Please try again.');
      this.isProcessing.set(false);
      this.stopProgressPolling();
    } finally {
      // NOTE: isProcessing / polling are intentionally NOT touched here on
      // the success path — completeCaseProcessing() (called from polling)
      // owns closing the modal once the backend confirms completion.
      console.groupEnd();
    }
  }

  /** Max attempts per chunk (1 initial + 2 retries) */
  private readonly MAX_CHUNK_ATTEMPTS = 3;

  /** Base delay in ms for exponential backoff (1s → 2s → 4s) */
  private readonly RETRY_BASE_DELAY_MS = 1000;

  // ─── Chunk upload with retry ─────────────────────────────────────────────

  /**
   * Sends a single chunk to the backend with exponential backoff retry.
   * Attempts: 1st try → wait 1s → 2nd try → wait 2s → 3rd try → throw.
   */
  private async sendOneChunk(
    form: FormData,
    chunkMeta: ChunkMetadata,
    totalChunks: number,
  ): Promise<{
    chunkNumber: number;
    hashVerified: boolean;
    totalChunks: number;
    chunksReceivedSoFar: number;
    allChunksReceived: boolean;
  }> {

    let lastError: unknown;

    for (let attempt = 1; attempt <= this.MAX_CHUNK_ATTEMPTS; attempt++) {
      try {
        if (attempt > 1) {
          const delay = this.RETRY_BASE_DELAY_MS * Math.pow(2, attempt - 2); // 1s, 2s, 4s…
          console.warn(
            `%c⏳ Chunk ${chunkMeta.chunkNumber}/${totalChunks} — attempt ${attempt}/${this.MAX_CHUNK_ATTEMPTS} (waiting ${delay}ms…)`,
            'color: #fb923c; font-weight: bold;'
          );
          await new Promise(r => setTimeout(r, delay));
        }

        const result = await firstValueFrom(
          this.http.post<{
            uploadId: string;
            chunkNumber: number;
            size: number;
            hash: string;
            hashVerified: boolean;
            totalChunks: number;
            chunksReceivedSoFar: number;
            allChunksReceived: boolean;
          }>(`${environment.apiBaseUrl}/api/uploads/chunk`, form, {
            withCredentials: true,
          })
        );
        console.log("First value received1")
        return result;   // success — exit retry loop

      } catch (err) {
        lastError = err;
        console.error(
          `%c❌ Chunk ${chunkMeta.chunkNumber}/${totalChunks} — attempt ${attempt}/${this.MAX_CHUNK_ATTEMPTS} failed:`,
          'color: #f87171; font-weight: bold;',
          err
        );
      }
    }

    // All attempts exhausted
    throw new Error(
      `Chunk ${chunkMeta.chunkNumber} failed after ${this.MAX_CHUNK_ATTEMPTS} attempts.`
    );
  }

  /**
   * Iterates over every chunk, builds FormData, and sends with retry.
   * Logs detailed metadata + Blob before each send.
   * Throws at the end if any chunks permanently failed.
   * NOTE: successfully sending all chunks does NOT mean processing is done —
   * that's reported later via progress polling.
   */
  private async sendChunks(file: File, metadata: FileUploadMetadata): Promise<void> {
    console.group(`🚀 Uploading ${metadata.totalChunks} chunk(s) for uploadId=${metadata.uploadId}`);

    const failedChunks: number[] = [];

    for (const chunkMeta of metadata.chunks) {
      const start     = (chunkMeta.chunkNumber - 1) * this.CHUNK_SIZE;
      const end       = start + chunkMeta.size;
      const chunkBlob = file.slice(start, end, file.type);

      console.group(
        `%c📤 Chunk ${chunkMeta.chunkNumber}/${metadata.totalChunks} — sending…`,
        'color: #facc15; font-weight: bold;'
      );
      console.log('Metadata :', {
        chunkNumber : chunkMeta.chunkNumber,
        byteRange   : `${start} – ${end - 1}`,
        size        : this.formatFileSize(chunkMeta.size),
        sizeBytes   : chunkMeta.size,
        hash        : chunkMeta.hash,
        uploadId    : metadata.uploadId,
      });
      console.log('Blob     :', chunkBlob);

      const form = new FormData();
      form.append('uploadId',    metadata.uploadId);
      form.append('chunkNumber', String(chunkMeta.chunkNumber));
      form.append('chunk',       chunkBlob, `chunk_${chunkMeta.chunkNumber}`);

      try {
        const result = await this.sendOneChunk(form, chunkMeta, metadata.totalChunks);
        console.log(
          `%c✅ Chunk ${result.chunkNumber}/${result.totalChunks} accepted` +
          ` — hashVerified=${result.hashVerified}` +
          ` — progress: ${result.chunksReceivedSoFar}/${result.totalChunks}`,
          'color: #4ade80; font-weight: bold;'
        );
        if (result.allChunksReceived) {
          this.progressMessage.set('Upload complete. Starting case-file analysis…');
          console.log(
            '%c🎉 Backend confirmed: all chunks received!',
            'color: #a78bfa; font-size: 14px; font-weight: bold;'
          );
        }
      } catch (err) {
        console.error(
          `%c💀 Chunk ${chunkMeta.chunkNumber} permanently failed — will report at end.`,
          'color: #f87171; font-weight: bold;'
        );
        failedChunks.push(chunkMeta.chunkNumber);
      }

      console.groupEnd();
      await new Promise(r => setTimeout(r, 50));  // small gap keeps logs readable
    }

    if (failedChunks.length === 0) {
      console.log(`%c🎉 All ${metadata.totalChunks} chunk(s) sent successfully!`, 'color: #4ade80; font-weight: bold;');
      // No throw here — "all chunks sent" is just an upload milestone.
      // Completion/failure of case processing is reported via polling.
    } else {
      const msg = `Upload incomplete — ${failedChunks.length} chunk(s) failed permanently: [${failedChunks.join(', ')}]`;
      console.error(`%c❌ ${msg}`, 'color: #f87171; font-weight: bold;');
      console.groupEnd();
      throw new Error(msg);
    }

    console.groupEnd();
  }

  // ─── Progress Polling ─────────────────────────────────────────────────────

  private startProgressPolling(uploadId: string): void {
    this.stopProgressPolling();
    const refresh = () => {
      this.http
        .get<{ events: UploadProgressEvent[] }>(
          `${environment.apiBaseUrl}/api/uploads/${uploadId}/progress`,
        )
        .subscribe({
          next: ({ events }) => {
            this.progressEvents.set(events);
            const latest = events.at(-1);
            if (!latest) return;

            this.progressMessage.set(latest.message);

            if (latest.state === 'complete') {
              this.stopProgressPolling();
              this.completeCaseProcessing();
            } else if (latest.state === 'failed') {
              this.stopProgressPolling();
              this.isProcessing.set(false);
              alert(`Case processing failed: ${latest.message}`);
            }
          },
          error: (error) => console.warn('Could not refresh upload progress:', error),
        });
    };
    refresh();
    this.progressPollId = window.setInterval(refresh, 700);
  }

  private stopProgressPolling(): void {
    if (this.progressPollId !== undefined) {
      window.clearInterval(this.progressPollId);
      this.progressPollId = undefined;
    }
  }

  /** Close the modal only after polling confirms the backend finished processing. */
  private completeCaseProcessing(): void {
    this.progressMessage.set('Case processing completed successfully.');
    this.isProcessing.set(false);
    this.stopProgressPolling();
    this.selectedFile.set(null);
    this.inspectorName.set('');
    this.inspectorRank.set('');
    this.inspectorBranch.set('');
    this.newCaseData.notifyCaseCreated();
    this.newCaseData.closeModal();
  }
}
