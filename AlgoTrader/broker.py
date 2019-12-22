import datetime
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, List, DefaultDict, Optional, Union, Any, Tuple, Set

from AlgoTrader.portfolio import Portfolio
from AlgoTrader.position import Position
from AlgoTrader.types import PortfolioID, Ticker, PositionID, TransactionType


# TODO: Create local transaction log (which syncs with the database, ideally asynchronously) and keep running totals.
# This would allow for quick calculation of portfolio balances both locally and on the database (at the moment it takes
# ~100ms, which in the long run becomes the main bottleneck taking up ~40% of the execution time). Fetching portfolio
# balances from the database (due floating precision inaccuracies the local and database value can diverge) will boil
# down to simply fetching the latest transaction for a given portfolio which can be done fast (~4ms) with the following:
# SELECT
# 	*
# FROM transactions
# WHERE portfolio_id = 13
# 	AND transactions.timestamp = (SELECT MAX(timestamp)
# 								   FROM transactions
# 								   WHERE transactions.portfolio_id = 13)
# ORDER BY id DESC
# LIMIT 1;.
#
class Broker:
    """A broker manages portfolios and executes buy/sell orders on behalf of traders."""

    def __init__(self, spx_changes, database_connection: sqlite3.Connection):
        """
        Create a new broker.

        :param database_connection: A connection to a database that can be queried for stock price data.
        """
        self.yesterday = datetime.datetime.fromtimestamp(0.0)
        self.today = datetime.datetime(year=self.yesterday.year, month=self.yesterday.month, day=self.yesterday.day + 1)

        self.stock_data: Dict[Ticker, Dict[str, Any]] = dict()
        self.yesterdays_stock_data: Dict[Ticker, Dict[str, Any]] = dict()
        self.last_known_prices: Dict[Ticker, Dict[str, float]] = dict()

        self.spx_changes: Dict[str, Dict[str, Dict[str: str]]] = spx_changes

        self.db_connection = database_connection
        # TODO: Read portfolios and positions from database?
        self.portfolios: Dict[PortfolioID, Portfolio] = dict()

        self.position_by_id: Dict[PositionID, Position] = dict()
        self.positions_by_ticker: DefaultDict[Ticker, List[Position]] = defaultdict(lambda: [])

    def seed_data(self, date: datetime.datetime):
        """
        Seed the broker's stock data.

        :param date: The date of the stock data that should be fetched.
        """
        self.yesterday = self.today
        self.today = date

        self._fetch_daily_data()

    def _fetch_daily_data(self):
        cursor = self.db_connection.execute(
            '''
            SELECT 
                ticker, datetime, open, close, macd_histogram, macd_line, signal_line
            FROM daily_stock_data
            WHERE datetime = ?
            ''',
            (self.today,)
        )

        self.yesterdays_stock_data = self.stock_data
        self.stock_data = {row['ticker']: {key: row[key] for key in row.keys()} for row in cursor}

        for ticker in self.stock_data:
            self.last_known_prices[ticker] = self.stock_data[ticker]

        cursor.close()

    def create_portfolio(self, owner_name: str, initial_contribution: float = 0.00) -> PortfolioID:
        """
        Create a new portfolio .

        :param owner_name: The name of the entity that the portfolio is being created for.
        :param initial_contribution: How much cash the portfolio should start with.
        :return: The ID of the created portfolio.
        """
        portfolio = Portfolio(owner_name, self.today, self.db_connection)
        self.portfolios[portfolio.id] = portfolio

        self._execute_transaction(TransactionType.DEPOSIT, portfolio.id, initial_contribution)

        return portfolio.id

    def add_contribution(self, amount: float, portfolio_id: PortfolioID):
        """
        Add an amount of cash to the given portfolio as an contribution.

        :param amount: The amount of cash to add.
        :param portfolio_id: The portfolio to add the cash to.
        """
        self._execute_transaction(TransactionType.DEPOSIT, portfolio_id, amount)

    def get_balance(self, portfolio_id: PortfolioID) -> float:
        """
        Get the balance of a given user's portfolio.

        :param portfolio_id: The ID of the portfolio.
        :return: The available balance of the portfolio.
        """
        return self.portfolios[portfolio_id].balance

    def get_open_positions(self, portfolio_id: PortfolioID) -> Set[Position]:
        """
        Get the open positions for the given portfolio.

        :param portfolio_id: The portfolio to check for open positions.
        :return: A set of open positions.
        """
        # return self.open_positions_by_portfolio[portfolio_id].copy()
        return self.portfolios[portfolio_id].open_positions

    def get_quote(self, ticker) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Get a quote for a security.

        :param ticker: The ticker of the security to get data for.
        :return: A 2-tuple containing: today's data and the previous day's data for the security, respectively.
        :raise KeyError: If data for the security is not available.
        """
        return self.stock_data[ticker], self.yesterdays_stock_data[ticker]

    # TODO: Allow for future orders (i.e. actual buy orders).
    def execute_buy_order(self, ticker: Ticker, quantity: int, portfolio_id: PortfolioID,
                          price: Union[str, float] = 'market_price'):
        """
        Execute a buy order at the market price.

        :param ticker: The ticker of the security to buy.
        :param quantity: How many shares to buy.
        :param portfolio_id: The portfolio to add the new position to.
        :param price: The price to but into the position at. By default, this is set to the current market price.
        """
        if price == 'market_price':
            price = self.last_known_prices[ticker]['close']
        else:
            price = float(price)

        self._execute_transaction(TransactionType.BUY, portfolio_id, price, quantity, ticker=ticker)

    def close_position(self, position: Position, price: Union[str, float] = 'market_price'):
        """
        Close a position.

        :param position: The position to close.
        :param price: The price to close out the position at. By default, this is set to the current market price.
        """
        if price == 'market_price':
            price = self.last_known_prices[position.ticker]['close']
        else:
            price = float(price)

        self._execute_transaction(TransactionType.SELL, position.portfolio_id, price, position_id=position.id)

    def update(self, now: datetime.datetime):
        """
        Perform an update step for the broker.

        This includes adjusting positions for dividends and stock splits.

        :param now: The date and time that should be considered to be 'now'. This affects what data is used.
        data.
        """
        self.yesterday = self.today
        self.today = now

        self._fetch_daily_data()
        pay_the_taxman_date = datetime.datetime(year=self.today.year, month=4, day=15)

        if self.yesterday < pay_the_taxman_date <= self.today:
            for portfolio in self.portfolios.values():
                tax_report = portfolio.generate_tax_report(self.today)
                self._execute_transaction(TransactionType.TAX, portfolio.id, tax_report.total_tax)
                print(tax_report)

        if str(self.today) in self.spx_changes:
            ticker = self.spx_changes[str(self.today)]['removed']['ticker']

            # We close any positions that trade in securities that have been taken off SPX as a quick fix.
            # TODO: Only close positions if a company has been delisted.
            if len(ticker) > 0:
                for position in filter(lambda p: not p.is_closed, self.positions_by_ticker[ticker]):
                    self.close_position(position)

        cursor = self.db_connection.execute(
            '''
            SELECT ticker, dividend_amount, split_coefficient, open 
            FROM daily_stock_data
            WHERE datetime=? AND (dividend_amount > 0 OR split_coefficient > 0);
            ''',
            (now,)
        )

        for row in cursor:
            if row['dividend_amount'] > 0:
                # TODO: Only pay dividend for shares that were owned prior to the ex-dividend date.
                # TODO: Get data for ex-dividend dates.
                for position in filter(lambda p: not p.is_closed, self.positions_by_ticker[row['ticker']]):
                    self._execute_transaction(TransactionType.DIVIDEND, position.portfolio_id, row['dividend_amount'],
                                              position_id=position.id)

            if abs(row['split_coefficient'] - 1) > sys.float_info.epsilon:  # roughly equal to
                # Need to make list here to avoid positions being added during stock split which the filter then
                # iterates up to, splitting that stock again, and again ad infinitum....
                positions = list(filter(lambda p: not p.is_closed, self.positions_by_ticker[row['ticker']]))

                for position in positions:
                    whole_shares, fractional_shares, adjusted_price, cash_settlement_amount = \
                        position.adjust_for_stock_split(row['split_coefficient'])

                    if cash_settlement_amount > 0:
                        self._execute_transaction(TransactionType.CASH_SETTLEMENT, position.portfolio_id,
                                                  cash_settlement_amount, position_id=position.id)

                    if whole_shares < 1:
                        self.close_position(position, price=0)
                    else:
                        self.close_position(position, price=position.entry_price)
                        self.execute_buy_order(position.ticker, int(whole_shares), position.portfolio_id,
                                               adjusted_price)

    def _execute_transaction(self, transaction_type: TransactionType, portfolio_id: PortfolioID, price: float,
                             quantity: Optional[int] = None, position_id: Optional[PositionID] = None,
                             ticker: Optional[Ticker] = None):
        """
        Execute a transaction.

        :param transaction_type: The type of transaction to execute. See `TransactionType` for available types.
        :param portfolio_id: The ID of the portfolio this transaction is being executed for.
        :param price: The price of the security being purchased/sold or the amount being deposited/withdrawn.
        :param quantity: (optional) The amount of shares being purchased. Must be specified for buy orders.
        :param position_id: (optional) The ID of the position to sell, or pay a dividend/cash settlement to. Must be
        specified for a: sell order, dividend payment or cash settlement.
        :param ticker: (optional) The ticker of the security to buy. Must be specified for buy orders.
        :return:
        """
        self._check_transaction_preconditions(transaction_type, ticker, quantity, position_id)

        portfolio = self.portfolios[portfolio_id]

        if transaction_type == TransactionType.DEPOSIT:
            portfolio.deposit(price)
        elif transaction_type == TransactionType.WITHDRAWAL:
            portfolio.withdraw(price)
        elif transaction_type == TransactionType.BUY:
            position = portfolio.open_position(ticker, price, quantity,
                                               self.today)

            position_id = position.id
            self.position_by_id[position.id] = position

            self.positions_by_ticker[ticker].append(position)
        elif transaction_type == TransactionType.SELL:
            position = self.position_by_id[position_id]
            portfolio.close_position(position, price, self.today)
        elif transaction_type == TransactionType.DIVIDEND:
            portfolio.pay_dividend(price, self.position_by_id[position_id])
        elif transaction_type == TransactionType.CASH_SETTLEMENT:
            portfolio.pay_cash_settlement(price, self.position_by_id[position_id])
        elif transaction_type == TransactionType.TAX:
            portfolio.deduct_taxes(price)

        if transaction_type in {TransactionType.SELL, TransactionType.DIVIDEND}:
            quantity = self.position_by_id[position_id].quantity
        elif transaction_type in {TransactionType.DEPOSIT, TransactionType.WITHDRAWAL, TransactionType.CASH_SETTLEMENT,
                                  TransactionType.TAX}:
            quantity = 1

        with self.db_connection:
            self.db_connection.execute(
                '''
                INSERT INTO transactions (portfolio_id, position_id, type, quantity, price, timestamp) 
                    VALUES (?, ?, (SELECT transaction_type.id FROM transaction_type WHERE name=?), ?, ?, ?)
                ''',
                (portfolio.id, position_id, transaction_type.name, quantity, price, self.today)
            )

    def _check_transaction_preconditions(self, transaction_type: TransactionType, ticker: Optional[Ticker],
                                         quantity: Optional[int], position_id: Optional[PositionID]):
        """
        Check that the given parameters a valid for a transaction to proceed.

        :raises AssertionError: if any conditions are not met.
        """
        if transaction_type in {TransactionType.SELL, TransactionType.DIVIDEND, TransactionType.CASH_SETTLEMENT}:
            assert position_id is not None, 'A position ID must specified for a: sell order, ' \
                                            'dividend payment or cash settlement payment.'
            assert position_id in self.position_by_id, f"Invalid position ID '{position_id}'."

        if transaction_type == TransactionType.BUY:
            assert quantity is not None, 'Quantity must be specified for a buy order.'

        if transaction_type == TransactionType.BUY:
            assert ticker is not None, 'A ticker must be specified for a buy order.'

    # TODO: Upload reports to database. This will allow for easy creation of plots such as equity vs. time. It also
    #  avoids creating slow views on the database side.
    def print_report(self, portfolio_id: PortfolioID, date: datetime.datetime):
        """
        Print a summary report of the given portfolio.

        :param portfolio_id: The ID of the portfolio to report on.
        :param date: The date the report was requested for. This affects the stock prices used in the valuation.
        """
        portfolio = self.portfolios[portfolio_id]
        portfolio.sync()
        portfolio.print_summary(date)
