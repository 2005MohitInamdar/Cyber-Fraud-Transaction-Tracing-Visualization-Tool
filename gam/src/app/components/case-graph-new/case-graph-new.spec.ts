import { ComponentFixture, TestBed } from '@angular/core/testing';

import { CaseGraphNew } from './case-graph-new';

describe('CaseGraphNew', () => {
  let component: CaseGraphNew;
  let fixture: ComponentFixture<CaseGraphNew>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CaseGraphNew],
    }).compileComponents();

    fixture = TestBed.createComponent(CaseGraphNew);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
