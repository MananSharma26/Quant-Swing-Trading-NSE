"""Order Management System (OMS) package.

Provides the order lifecycle infrastructure:
  - OrderStateMachine: enforces valid order status transitions.
  - OrderLedger: in-memory store for orders, fills, and risk decisions.
  - OrderManager: converts OrderIntents into InternalOrders via risk checking.

This package does NOT place real orders and does NOT call Zerodha APIs.
"""
