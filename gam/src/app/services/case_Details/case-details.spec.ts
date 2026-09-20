import { TestBed } from '@angular/core/testing';

import { CaseDetails } from './case-details';

describe('CaseDetails', () => {
  let service: CaseDetails;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    service = TestBed.inject(CaseDetails);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});
