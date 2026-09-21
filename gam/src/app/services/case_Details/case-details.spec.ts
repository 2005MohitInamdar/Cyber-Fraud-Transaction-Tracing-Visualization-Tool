import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';

import { Case_Details } from './case-details';

describe('Case_Details', () => {
  let service: Case_Details;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(Case_Details);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
