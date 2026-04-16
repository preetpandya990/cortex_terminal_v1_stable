'use client';

import { useAuth } from '@/contexts/AuthContext';
import { OpenPositionsPlaceholder } from "@/components/dashboard/OpenPositionsPlaceholder";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { TrendingUp, Brain, Activity, AlertTriangle } from "lucide-react";
import Link from "next/link";

export default function Home() {
  const { isAuthenticated, user } = useAuth();

  return (
    <div className="space-y-6">
      {/* Welcome Section */}
      <Card>
        <CardHeader>
          <CardTitle className="text-2xl">Welcome to Cortex Terminal V1</CardTitle>
          <CardDescription>
            {isAuthenticated 
              ? `Welcome back! Access your AI-powered trading tools below.`
              : 'Please log in to access the full trading platform.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* Cortex AI Card */}
            <Link href="/cortex-ai">
              <Card className="cursor-pointer hover:border-primary transition-colors">
                <CardHeader className="pb-3">
                  <div className="flex items-center gap-2">
                    <Brain className="h-5 w-5 text-primary" />
                    <CardTitle className="text-base">Cortex AI</CardTitle>
                  </div>
                </CardHeader>
                <CardContent>
                  <p className="text-sm text-muted-foreground">
                    AI-powered signals, regime detection, and market intelligence
                  </p>
                </CardContent>
              </Card>
            </Link>

            {/* Hawk Eye Radar Card */}
            <Link href="/hawk-eye-radar">
              <Card className="cursor-pointer hover:border-primary transition-colors">
                <CardHeader className="pb-3">
                  <div className="flex items-center gap-2">
                    <Activity className="h-5 w-5 text-primary" />
                    <CardTitle className="text-base">Hawk Eye Radar</CardTitle>
                  </div>
                </CardHeader>
                <CardContent>
                  <p className="text-sm text-muted-foreground">
                    Multi-timeframe signal scanner and technical analysis
                  </p>
                </CardContent>
              </Card>
            </Link>

            {/* Scanner Card */}
            <Link href="/scanner">
              <Card className="cursor-pointer hover:border-primary transition-colors">
                <CardHeader className="pb-3">
                  <div className="flex items-center gap-2">
                    <TrendingUp className="h-5 w-5 text-primary" />
                    <CardTitle className="text-base">Market Scanner</CardTitle>
                  </div>
                </CardHeader>
                <CardContent>
                  <p className="text-sm text-muted-foreground">
                    Scan markets for gainers, losers, and volume spikes
                  </p>
                </CardContent>
              </Card>
            </Link>

            {/* Risk Management Card */}
            <Card className="cursor-pointer hover:border-muted transition-colors opacity-60">
              <CardHeader className="pb-3">
                <div className="flex items-center gap-2">
                  <AlertTriangle className="h-5 w-5 text-muted-foreground" />
                  <CardTitle className="text-base">Risk Management</CardTitle>
                </div>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">
                  Portfolio risk analysis and position sizing (Coming Soon)
                </p>
              </CardContent>
            </Card>
          </div>

          {!isAuthenticated && (
            <div className="mt-6 p-4 bg-muted rounded-lg">
              <p className="text-sm text-muted-foreground text-center">
                Log in to access all features and start trading with AI-powered insights
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Open Positions */}
      {isAuthenticated && <OpenPositionsPlaceholder />}
    </div>
  );
}
