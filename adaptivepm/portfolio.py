from __future__ import annotations

import os
import pickle
from dataclasses import dataclass, field
from typing import Dict, Iterator, List

import pandas as pd
import torch

from adaptivepm import Asset

PATH_TO_PRICES_PICKLE = os.path.join(
    os.getcwd(), "datasets", "Kraken_pipeline_output", "prices.pkl"
)


@dataclass
class Portfolio:
    """Implements a Portfolio that holds CryptoCurrencies as Assets
    Parameters
    _ _ _ _ _ _ _ _ _ _


    Attributes:
    _ _ _ _ _ _ __ __ _ _
    __assets: Dict[name, Asset]
        dictionary of Asset objects whose keys are the asset names
    """

    asset_names: List[str]
    __prices: Dict[str, pd.DataFrame] = field(init=False, default_factory=lambda: {})
    __assets: Dict[str, Asset] = field(init=False)
    m_assets: int = field(init=False, default=0)
    m_noncash_assets: int = field(init=False, default=0)

    def __post_init__(self):
        self._load_pickle_object()
        self.__assets = {
            asset_name: Asset(
                name=asset_name,
                open_price=self.__prices["open"][asset_name],
                close_price=self.__prices["close"][asset_name],
                high_price=self.__prices["high"][asset_name],
                low_price=self.__prices["low"][asset_name],
            )
            for asset_name in self.asset_names
        }
        self.m_assets = len(self.__assets)
        self.m_noncash_assets = self.m_assets - 1
        self.n_samples = self.__prices["close"].shape[0]

    def _load_pickle_object(self):
        with open(PATH_TO_PRICES_PICKLE, "rb") as f:
            self.__prices.update(pickle.load(f))

    def __iter__(self) -> Iterator[Asset]:
        yield from self.assets()

    def __repr__(self) -> str:
        return f"Portfolio size: {self.m_assets} \
            \nm_assets: {[asset.name for asset in self.assets()]}"

    def get_asset(self, name: str) -> Asset:
        """Returns the asset in the portfolio given the name of the asset

        Parameters
        ----------
        asset : str
            name of the asset

        Returns
        -------
        Asset
            contains information about the asset
        """
        return self.__assets.get(name.upper())

    def assets(self) -> Iterator[Asset]:
        yield from self.__assets.values()

    def get_relative_price(self):
        return self.__prices["relative_price"]

    def get_close_price(self):
        return self.__prices["close"]

    def get_high_price(self):
        return self.__prices["high"]

    def get_low_price(self):
        return self.__prices["low"]

    def get_end_of_period_weights(self, yt: torch.tensor, wt_prev: torch.tensor):
        """Computes the wt' which is portfolio weight at the end of period t
        c.f formula 7 in https://arxiv.org/pdf/1706.10059

        Parameters
        ----------
        yt : torch.tensor
            relative price vector representing market movement from t-1 to period t
            given by close_t / close(t-1)
            shape=(batch_size, m_noncash_assets)
        wt_prev : torch.tensor
            portfolio weight at the beginning of previous period
            shape=(batch_size, m_noncash_assets)
        """
        batch_size = wt_prev.shape[0]
        wt_prime = (yt * wt_prev) / (yt * wt_prev).sum(dim=1).view(batch_size, 1)
        return wt_prime

    def get_transacton_remainder_factor(
        self,
        wt: torch.tensor,
        yt: torch.tensor,
        wt_prev: torch.tensor,
        comission_rate: float = 0.0026,
        n_iter: int = 3,
    ):
        """Computes the transaction remainder factor via a iterative approach
        c.f formula 14 and 15 in https://arxiv.org/pdf/1706.10059
        This formula reduces the portfolio value from pt' to pt

        Parameters
        ----------
         Parameters
        ----------
        wt : torch.tensor
            portfolio vector weight at beginning of period t+1
            dim=(batch_size, m_noncash_assets)
        yt : torch.tensor
            relative price vector given by close_t / close_t-1
            dim=(batch_size, m_noncash_assets)
        wt_prev : torch.tensor
            portfolio vector weight at beginning of period t
            dim=(batch_size, m_noncash_assets)
        comission_rate : float, default = 0.26% (maximum)
            comission rate for purchasing and selling
        n_iter: int, default = 3
        number of iterations to compute the transaction remainder factor
        """
        wt_prime = self.get_end_of_period_weights(yt, wt_prev)

        # get end of period cash position for each example in batch
        wt_cash_prime = 1 - wt_prime.sum(dim=1)

        # get cash position for portfolio weight at period t+1
        wt_cash = 1 - wt.sum(dim=1)

        # initial transaction remainder factor
        ut_k = comission_rate * torch.abs(wt - wt_prime).sum(dim=1)
        c = comission_rate
        for _ in range(n_iter):
            update_term = torch.relu(wt_prime - ut_k.unsqueeze(1) * wt).sum(dim=1)
            ut_k = (
                1
                / (1 - c * wt_cash)
                * (1 - c * wt_cash_prime - c * (2 - c) * update_term)
            )
        return ut_k

    def get_reward(self, wt: torch.tensor, yt: torch.tensor, wt_prev: torch.tensor):
        """_summary_

        Parameters
        ----------
        wt : torch.tensor
            portfolio vector for beginning of period t+1
        yt : torch.tensor
            relative price vector given by Close_t / Close(t-1)
        wt_prev : torch.tensor
            portfolio vector weight for beginning of period t
        """
        batch_size = wt.shape[0]
        ut = self.get_transacton_remainder_factor(wt, yt, wt_prev)

        # portfolio return before transaction cost
        portfolio_return = (yt * wt_prev).sum(dim=1)
        rt = torch.log(ut * portfolio_return)

        return rt / batch_size


if __name__ == "__main__":
    # used for debugging purposes
    m_assets: List[str] = [
        "CASH",
        "SOL",
        "ADA",
        "USDT",
        "AVAX",
        "LINK",
        "DOT",
        "PEPE",
        "ETH",
        "XRP",
        "TRX",
        "MATIC",
    ]
    port = Portfolio(asset_names=m_assets)
    print(port)
