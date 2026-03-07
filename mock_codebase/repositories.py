"""Repository pattern — data access layer."""

from abc import ABC, abstractmethod
from typing import Optional

from mock_codebase.models import Product, Customer, Order


class Repository(ABC):
    """Abstract repository interface."""

    @abstractmethod
    def save(self, entity) -> None:
        ...

    @abstractmethod
    def find_by_id(self, entity_id: str):
        ...

    @abstractmethod
    def find_all(self) -> list:
        ...

    @abstractmethod
    def delete(self, entity_id: str) -> None:
        ...


class ProductRepository(Repository):
    def __init__(self):
        self._store: dict[str, Product] = {}

    def save(self, product: Product) -> None:
        product.validate()
        self._store[product.id] = product

    def find_by_id(self, product_id: str) -> Optional[Product]:
        return self._store.get(product_id)

    def find_all(self) -> list[Product]:
        return list(self._store.values())

    def find_by_category(self, category: str) -> list[Product]:
        return [p for p in self._store.values() if p.category == category]

    def delete(self, product_id: str) -> None:
        self._store.pop(product_id, None)


class CustomerRepository(Repository):
    def __init__(self):
        self._store: dict[str, Customer] = {}

    def save(self, customer: Customer) -> None:
        customer.validate()
        self._store[customer.id] = customer

    def find_by_id(self, customer_id: str) -> Optional[Customer]:
        return self._store.get(customer_id)

    def find_all(self) -> list[Customer]:
        return list(self._store.values())

    def find_by_email(self, email: str) -> Optional[Customer]:
        for c in self._store.values():
            if c.email == email:
                return c
        return None

    def delete(self, customer_id: str) -> None:
        self._store.pop(customer_id, None)


class OrderRepository(Repository):
    def __init__(self):
        self._store: dict[str, Order] = {}

    def save(self, order: Order) -> None:
        order.validate()
        self._store[order.id] = order

    def find_by_id(self, order_id: str) -> Optional[Order]:
        return self._store.get(order_id)

    def find_all(self) -> list[Order]:
        return list(self._store.values())

    def find_by_customer(self, customer_id: str) -> list[Order]:
        return [
            o for o in self._store.values() if o.customer.id == customer_id
        ]

    def delete(self, order_id: str) -> None:
        self._store.pop(order_id, None)

