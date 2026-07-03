export type AIProcessingCategory = 'sentiment' | 'forecast' | 'classification'

export interface CategoryStatus {
  pending: number
  auto_flush: boolean
}

export interface AIProcessingStatusResponse {
  sentiment: CategoryStatus
  forecast: CategoryStatus
  classification: CategoryStatus
}

export interface DispatchResult {
  dispatched: number
  calls_made: number
}
