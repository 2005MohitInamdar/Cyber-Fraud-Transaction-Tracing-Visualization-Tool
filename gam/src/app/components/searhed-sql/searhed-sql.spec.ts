import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';

import { SearhedSQL } from './searhed-sql';

describe('SearhedSQL', () => {
  let component: SearhedSQL;
  let fixture: ComponentFixture<SearhedSQL>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SearhedSQL],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(SearhedSQL);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
